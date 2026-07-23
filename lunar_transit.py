# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 MaxBotev
"""
LunarTransit — aircraft/Moon transit prediction engine.

Runs as a background thread inside the UFO Monitor process. Every second it:
  1. reads live aircraft from dump1090's aircraft.json (/dev/shm/ufo_adsb),
  2. projects each trajectory forward ~90 s (great-circle at current gs/track,
     plus baro climb rate),
  3. computes topocentric az/el for the aircraft (WGS-84 ECEF -> ENU) and the
     Moon (Skyfield, DE421), and the angular separation between them,
  4. fires a Telegram alert when a transit (or near miss) is predicted, and
  5. arms a TCP capture trigger: "REC\n" at T-pre and "STOP\n" at T+post to the
     SharpCap listener on the capture PC. No human in the loop.

Config keys (config.json, all optional — defaults below):
  home_alt_m            observer altitude, metres            (60)
  lunar_enabled         master switch                        (true)
  lunar_margin_deg      extra margin beyond Moon radius      (0.10)
  lunar_watch_deg       "near miss" heads-up zone            (2.0)
  lunar_min_elev_deg    ignore Moon below this elevation     (10)
  lunar_notify          Telegram alerts on/off               (true)
  capture_enabled       TCP trigger on/off                   (false)
  capture_host          SharpCap listener host               ("")
  capture_port          SharpCap listener port               (5580)
  capture_pre_s         start recording this before TCA      (20)
  capture_post_s        stop recording this after TCA        (20)
"""

import json
import math
import os
import socket
import threading
import time
from collections import deque

import numpy as np

from notify import TelegramNotifier

ADSB_JSON = "/dev/shm/ufo_adsb/aircraft.json"
EPHEMERIS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "de421.bsp")
EVENT_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lunar_events.jsonl")

MOON_RADIUS_KM = 1737.4
WGS84_A = 6378137.0
WGS84_F = 1.0 / 298.257223563
WGS84_E2 = WGS84_F * (2.0 - WGS84_F)
KT_TO_MS = 0.514444
FT_TO_M = 0.3048
HORIZON_S = 90          # how far ahead to project trajectories
STEP_S = 1.0            # projection step
PATH_ZONE_DEG = 3.0     # include sky-path polyline for aircraft this close

def load_horizon(path):
    """NINA custom-horizon file: 'azimuth altitude' pairs, # comments."""
    pts = []
    try:
        for line in open(path):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                pts.append((float(parts[0]) % 360.0, float(parts[1])))
    except Exception:
        return None
    pts.sort()
    return pts if len(pts) >= 3 else None


def horizon_alt_at(pts, az):
    """Local horizon altitude at azimuth (linear interp, wraps 360)."""
    if not pts:
        return 0.0
    az = float(az) % 360.0
    azs = [p[0] for p in pts]
    import bisect
    i = bisect.bisect_left(azs, az)
    a0, h0 = pts[i - 1] if i > 0 else (pts[-1][0] - 360.0, pts[-1][1])
    a1, h1 = pts[i] if i < len(pts) else (pts[0][0] + 360.0, pts[0][1])
    if a1 == a0:
        return h0
    f = (az - a0) / (a1 - a0)
    return h0 + f * (h1 - h0)


DEFAULTS = {
    "home_alt_m": 60.0,
    "lunar_enabled": True,
    "lunar_margin_deg": 0.10,
    "lunar_watch_deg": 2.0,
    "lunar_min_elev_deg": 10.0,
    "lunar_notify": True,
    "capture_enabled": False,
    "capture_host": "",
    "capture_port": 5580,
    "capture_pre_s": 20.0,
    "capture_post_s": 20.0,
    "horizon_file": "",             # NINA-format local horizon (optional)
    "horizon_margin_deg": 10.0,      # lower the effective horizon by this many
                                    # deg for ALERTS (bigger = less strict; the
                                    # drawn skyline still shows the true file)
}


def geodetic_to_ecef(lat_deg, lon_deg, alt_m):
    """WGS-84 geodetic -> ECEF (metres). Accepts numpy arrays."""
    lat = np.radians(lat_deg)
    lon = np.radians(lon_deg)
    s, c = np.sin(lat), np.cos(lat)
    n = WGS84_A / np.sqrt(1.0 - WGS84_E2 * s * s)
    x = (n + alt_m) * c * np.cos(lon)
    y = (n + alt_m) * c * np.sin(lon)
    z = (n * (1.0 - WGS84_E2) + alt_m) * s
    return np.stack([x, y, z], axis=-1)


class Observer:
    """Fixed site: precomputed ECEF origin + ENU rotation."""

    def __init__(self, lat, lon, alt_m):
        self.lat, self.lon, self.alt_m = lat, lon, alt_m
        self.ecef = geodetic_to_ecef(np.float64(lat), np.float64(lon), np.float64(alt_m))
        la, lo = math.radians(lat), math.radians(lon)
        sl, cl = math.sin(la), math.cos(la)
        so, co = math.sin(lo), math.cos(lo)
        # rows: east, north, up
        self.enu = np.array([
            [-so, co, 0.0],
            [-sl * co, -sl * so, cl],
            [cl * co, cl * so, sl],
        ])

    def unit_vectors(self, ecef_pts):
        """ECEF points (N,3) -> unit look vectors in ENU (N,3)."""
        d = np.atleast_2d(ecef_pts) - self.ecef
        v = d @ self.enu.T
        return v / np.linalg.norm(v, axis=-1, keepdims=True)

    @staticmethod
    def azel(unit):
        u = np.atleast_2d(unit)
        az = np.degrees(np.arctan2(u[:, 0], u[:, 1])) % 360.0
        el = np.degrees(np.arcsin(np.clip(u[:, 2], -1, 1)))
        return az, el

    @staticmethod
    def azel_to_unit(az_deg, el_deg):
        az, el = np.radians(az_deg), np.radians(el_deg)
        ce = np.cos(el)
        return np.stack([ce * np.sin(az), ce * np.cos(az), np.sin(el)], axis=-1)


def project_track(lat, lon, alt_m, gs_ms, track_deg, vrate_ms, times_s):
    """Dead-reckon a trajectory forward. Returns geodetic arrays (N,)."""
    t = np.asarray(times_s, dtype=float)
    dist = gs_ms * t
    brg = math.radians(track_deg)
    r = 6371000.0 + alt_m
    dlat = dist * math.cos(brg) / r
    dlon = dist * math.sin(brg) / (r * math.cos(math.radians(lat)))
    return (lat + np.degrees(dlat),
            lon + np.degrees(dlon),
            np.maximum(alt_m + vrate_ms * t, 0.0))


class CaptureTrigger:
    """Schedules REC/STOP over TCP to the SharpCap listener."""

    def __init__(self):
        self.lock = threading.Lock()
        self.armed_hex = None
        self.rec_at = None
        self.stop_at = None
        self.rec_sent = False
        self.last_result = "idle"

    def _send(self, host, port, msg):
        try:
            with socket.create_connection((host, int(port)), timeout=5) as s:
                s.sendall((msg + "\n").encode())
                s.settimeout(3)
                try:
                    reply = s.recv(64).decode(errors="replace").strip()
                except Exception:
                    reply = ""
            return True, reply or "sent"
        except Exception as e:
            return False, str(e)

    def arm(self, hexid, tca_unix, pre_s, post_s):
        with self.lock:
            if self.armed_hex not in (None, hexid) and not self.rec_sent:
                # a different, earlier target wins; keep whichever TCA is sooner
                if self.rec_at is not None and tca_unix - pre_s >= self.rec_at:
                    return
            if self.armed_hex == hexid and self.rec_sent:
                # already recording this target — only push STOP later if needed
                self.stop_at = max(self.stop_at, tca_unix + post_s)
                return
            self.armed_hex = hexid
            self.rec_at = tca_unix - pre_s
            self.stop_at = tca_unix + post_s
            if not self.rec_sent:
                self.last_result = f"armed for {hexid}"

    def tick(self, now, cfg, log_event):
        host, port = cfg["capture_host"], cfg["capture_port"]
        with self.lock:
            if self.armed_hex is None:
                return
            if not cfg["capture_enabled"] or not host:
                self.armed_hex, self.rec_sent = None, False
                return
            if not self.rec_sent and now >= self.rec_at:
                ok, info = self._send(host, port, "REC")
                self.rec_sent = True
                self.last_result = f"REC {'ok' if ok else 'FAIL'}: {info}"
                log_event("capture", f"REC -> {host}:{port} ({info})", ok=ok)
            if self.rec_sent and now >= self.stop_at:
                ok, info = self._send(host, port, "STOP")
                self.last_result = f"STOP {'ok' if ok else 'FAIL'}: {info}"
                log_event("capture", f"STOP -> {host}:{port} ({info})", ok=ok)
                self.armed_hex, self.rec_sent = None, False

    def test(self, host, port):
        ok, info = self._send(host, port, "PING")
        return ok, info

    def snapshot(self, now):
        with self.lock:
            return {
                "armed_for": self.armed_hex,
                "recording": self.rec_sent,
                "rec_in_s": round(self.rec_at - now, 1) if self.rec_at and not self.rec_sent else None,
                "stop_in_s": round(self.stop_at - now, 1) if self.stop_at and self.rec_sent else None,
                "last_result": self.last_result,
            }


class LunarTransitEngine:
    def __init__(self, state):
        self.state = state          # UFO Monitor shared State (lock + cfg)
        self.lock = threading.Lock()
        self.snap = {"ok": False, "message": "starting"}
        self.events = deque(maxlen=80)
        self.alerted = {}           # hex -> {"watch": t, "transit": t}
        self.capture = CaptureTrigger()
        self.sim = None             # synthetic aircraft for end-to-end testing
        self.best = None            # cached best-dates for the current month
        self.horizon = None         # local-horizon profile [(az, alt), ...]
        self._hrz_mtime = None
        self._eph = None
        self._ts = None
        self._moon = self._earth = self._sun = None

    # ---- config ----------------------------------------------------------
    def cfg_val(self, key, default):
        with self.state.lock:
            return self.state.cfg.get(key, default)

    def cfg(self):
        with self.state.lock:
            c = dict(self.state.cfg)
        out = dict(DEFAULTS)
        for k in DEFAULTS:
            if k in c:
                out[k] = c[k]
        out["home_lat"] = c.get("home_lat", 0.0)
        out["home_lon"] = c.get("home_lon", 0.0)
        out["token"] = c.get("telegram_bot_token")
        out["chat"] = c.get("telegram_chat_id")
        out["telegram_enabled"] = c.get("telegram_enabled", True)
        return out

    # ---- events ----------------------------------------------------------
    def log_event(self, kind, text, **extra):
        ev = {"t": time.time(), "kind": kind, "text": text}
        ev.update(extra)
        with self.lock:
            self.events.appendleft(ev)
        try:
            with open(EVENT_LOG, "a") as f:
                f.write(json.dumps(ev) + "\n")
        except Exception:
            pass

    def notify(self, cfg, text):
        if cfg["lunar_notify"] and cfg["telegram_enabled"]:
            TelegramNotifier(cfg["token"], cfg["chat"]).send_async(text)

    # ---- skyfield --------------------------------------------------------
    def _init_sky(self):
        from skyfield.api import load
        self._eph = load(EPHEMERIS) if os.path.exists(EPHEMERIS) else None
        if self._eph is None:
            raise RuntimeError(f"ephemeris missing: {EPHEMERIS}")
        self._ts = load.timescale()
        self._earth = self._eph["earth"]
        self._moon = self._eph["moon"]
        self._sun = self._eph["sun"]

    def moon_azel(self, observer, unix_times):
        """Moon topocentric az/el(+distance) at given unix times."""
        from skyfield.api import wgs84
        from datetime import datetime, timezone
        site = self._earth + wgs84.latlon(observer.lat, observer.lon,
                                          elevation_m=observer.alt_m)
        t = self._ts.from_datetimes(
            [datetime.fromtimestamp(u, tz=timezone.utc) for u in unix_times])
        app = site.at(t).observe(self._moon).apparent()
        alt, az, dist = app.altaz()
        return az.degrees, alt.degrees, dist.km

    def compute_best_dates(self, observer, min_elev):
        """Score each evening (19:00–01:00 local) of the current month by how
        long the Moon is above min_elev and how illuminated it is."""
        import calendar
        from datetime import datetime, timedelta, timezone
        from skyfield.api import wgs84

        now = datetime.now().astimezone()
        tz = now.tzinfo
        year, month = now.year, now.month
        ndays = calendar.monthrange(year, month)[1]
        step_min = 10
        spd = int(6 * 60 / step_min) + 1     # samples per evening
        dts = []
        for d in range(1, ndays + 1):
            start = datetime(year, month, d, 19, 0, tzinfo=tz)
            dts.extend(start + timedelta(minutes=step_min * i) for i in range(spd))
        t = self._ts.from_datetimes([dt.astimezone(timezone.utc) for dt in dts])
        site = self._earth + wgs84.latlon(observer.lat, observer.lon,
                                          elevation_m=observer.alt_m)
        _aa = site.at(t).observe(self._moon).apparent().altaz()
        alt = _aa[0].degrees
        _az = _aa[1].degrees
        if self.horizon:
            _mg = self.cfg_val("horizon_margin_deg", 0.0)
            _hz = np.array([horizon_alt_at(self.horizon, a) - _mg for a in _az])
            alt = np.where(alt >= _hz, alt, -90.0)   # blocked = below

        dates = []
        for i, d in enumerate(range(1, ndays + 1)):
            seg = alt[i * spd:(i + 1) * spd]
            above = seg >= min_elev
            hours = float(above.sum()) * step_min / 60.0
            if hours < 1.0:
                continue
            first = int(np.argmax(above))
            last = len(above) - 1 - int(np.argmax(above[::-1]))
            t_from = dts[i * spd + first]
            t_to = dts[i * spd + last]
            _, illum = self.moon_illum(
                datetime(year, month, d, 21, 0, tzinfo=tz).timestamp())
            dates.append({
                "day": d,
                "label": t_from.strftime("%b %d").upper(),
                "dow": t_from.strftime("%a").upper()[:2],
                "illum": round(float(illum), 2),
                "max_el": round(float(seg.max())),
                "from": t_from.strftime("%H:%M"),
                "to": t_to.strftime("%H:%M"),
                "hours": round(hours, 1),
                "score": hours * (0.25 + 0.75 * float(illum)),
            })
        return {"month": f"{year}-{month:02d}", "month_name": now.strftime("%B").upper(),
                "dates": dates}

    def best_dates_snapshot(self):
        """Top remaining evenings this month, chronological."""
        if not self.best:
            return None
        from datetime import datetime
        today = datetime.now().astimezone().day
        future = [d for d in self.best["dates"] if d["day"] >= today]
        top = sorted(future, key=lambda d: -d["score"])[:5]
        return {"month": self.best["month_name"],
                "dates": sorted(top, key=lambda d: d["day"])}

    def sun_azel(self, observer, unix_time):
        """Sun topocentric az/el (used for 3D moon-phase lighting)."""
        from skyfield.api import wgs84
        from datetime import datetime, timezone
        site = self._earth + wgs84.latlon(observer.lat, observer.lon,
                                          elevation_m=observer.alt_m)
        t = self._ts.from_datetime(
            datetime.fromtimestamp(unix_time, tz=timezone.utc))
        alt, az, _ = site.at(t).observe(self._sun).apparent().altaz()
        return float(az.degrees), float(alt.degrees)

    def moon_illum(self, unix_time):
        from datetime import datetime, timezone
        t = self._ts.from_datetime(datetime.fromtimestamp(unix_time, tz=timezone.utc))
        e = self._earth.at(t)
        _, slon, _ = e.observe(self._sun).apparent().ecliptic_latlon()
        _, mlon, _ = e.observe(self._moon).apparent().ecliptic_latlon()
        phase = (mlon.degrees - slon.degrees) % 360.0
        return phase, (1.0 - math.cos(math.radians(phase))) / 2.0

    # ---- aircraft input ---------------------------------------------------
    def read_aircraft(self):
        try:
            age = time.time() - os.path.getmtime(ADSB_JSON)
            with open(ADSB_JSON) as f:
                data = json.load(f)
        except Exception:
            return [], None
        out = []
        for a in data.get("aircraft", []):
            lat, lon = a.get("lat"), a.get("lon")
            gs, trk = a.get("gs"), a.get("track")
            alt = a.get("alt_geom", a.get("alt_baro"))
            if None in (lat, lon, gs, trk) or not isinstance(alt, (int, float)):
                continue
            if (a.get("seen_pos") or a.get("seen") or 0) > 15:
                continue
            out.append({
                "hex": a.get("hex"), "flight": (a.get("flight") or "").strip(),
                "lat": lat, "lon": lon, "alt_ft": alt,
                "gs_kt": gs, "track": trk,
                "vrate_fpm": a.get("baro_rate") or a.get("geom_rate") or 0,
            })
        return out, age

    # ---- simulation -------------------------------------------------------
    def start_simulation(self, observer, moon_az, moon_el, lead_s=60):
        """Inject a fake aircraft that will cross the Moon's disc in ~lead_s."""
        alt_m = 10000.0
        el = math.radians(moon_el)
        ground_range = alt_m / math.tan(el) if el > 0.05 else 100000.0
        slant_lat_off = (ground_range * math.cos(math.radians(moon_az))) / 111320.0
        slant_lon_off = (ground_range * math.sin(math.radians(moon_az))) / (
            111320.0 * math.cos(math.radians(observer.lat)))
        # point on the Moon's line of sight; approach it from the south-west
        tgt_lat = observer.lat + slant_lat_off
        tgt_lon = observer.lon + slant_lon_off
        gs_ms = 220.0
        heading = 45.0
        back = gs_ms * lead_s
        lat0 = tgt_lat - math.degrees(back * math.cos(math.radians(heading)) / 6371000.0)
        lon0 = tgt_lon - math.degrees(back * math.sin(math.radians(heading)) /
                                      (6371000.0 * math.cos(math.radians(tgt_lat))))
        self.sim = {
            "hex": "SIM001", "flight": "SIMULATE", "t0": time.time(),
            "lat": lat0, "lon": lon0, "alt_ft": alt_m / FT_TO_M,
            "gs_kt": gs_ms / KT_TO_MS, "track": heading, "vrate_fpm": 0,
            "expires": time.time() + lead_s + 60,
        }
        self.log_event("sim", f"simulation started — synthetic transit in ~{lead_s}s")

    def sim_aircraft(self):
        s = self.sim
        if not s:
            return None
        if time.time() > s["expires"]:
            self.sim = None
            self.alerted.pop("SIM001", None)
            self.log_event("sim", "simulation ended")
            return None
        dt = time.time() - s["t0"]
        lat, lon, alt = project_track(s["lat"], s["lon"], s["alt_ft"] * FT_TO_M,
                                      s["gs_kt"] * KT_TO_MS, s["track"], 0.0, [dt])
        return {**{k: s[k] for k in ("hex", "flight", "gs_kt", "track", "vrate_fpm")},
                "lat": float(lat[0]), "lon": float(lon[0]),
                "alt_ft": float(alt[0] / FT_TO_M), "sim": True}

    # ---- main loop --------------------------------------------------------
    def run(self):
        try:
            self._init_sky()
        except Exception as e:
            with self.lock:
                self.snap = {"ok": False, "message": f"skyfield init failed: {e}"}
            return
        last_moon = 0.0
        moon = None
        observer = None
        while True:
            try:
                cfg = self.cfg()
                now = time.time()
                if not cfg["lunar_enabled"]:
                    with self.lock:
                        self.snap = {"ok": False, "message": "disabled in config"}
                    time.sleep(5)
                    continue
                hf = cfg.get("horizon_file") or ""
                if hf:
                    if not os.path.isabs(hf):
                        hf = os.path.join(os.path.dirname(os.path.abspath(__file__)), hf)
                    try:
                        mt = os.path.getmtime(hf)
                        if mt != self._hrz_mtime:
                            self.horizon = load_horizon(hf)
                            self._hrz_mtime = mt
                    except OSError:
                        self.horizon = None
                if (observer is None or observer.lat != cfg["home_lat"]
                        or observer.lon != cfg["home_lon"]
                        or observer.alt_m != cfg["home_alt_m"]):
                    observer = Observer(cfg["home_lat"], cfg["home_lon"], cfg["home_alt_m"])
                    last_moon = 0.0          # force moon recompute for new site
                # Moon: recompute every 30 s at t and t+HORIZON, interp between
                if moon is None or now - last_moon > 30:
                    az2, el2, dist2 = self.moon_azel(observer, [now, now + HORIZON_S])
                    phase, illum = self.moon_illum(now)
                    sun_az, sun_el = self.sun_azel(observer, now)
                    moon = {
                        "u0": Observer.azel_to_unit(az2[0], el2[0])[()],
                        "u1": Observer.azel_to_unit(az2[1], el2[1])[()],
                        "t0": now, "az": float(az2[0]), "el": float(el2[0]),
                        "dist_km": float(dist2[0]),
                        "radius_deg": math.degrees(math.asin(MOON_RADIUS_KM / dist2[0])),
                        "phase": float(phase), "illum": float(illum),
                        "sun_az": sun_az, "sun_el": sun_el,
                    }
                    last_moon = now
                month_key = time.strftime("%Y-%m")
                if self.best is None or self.best["month"] != month_key:
                    self.best = self.compute_best_dates(
                        observer, cfg["lunar_min_elev_deg"])
                self.step(cfg, observer, moon, now)
                self.capture.tick(now, cfg, self.log_event)
            except Exception as e:
                with self.lock:
                    self.snap = {"ok": False, "message": f"loop error: {e}"}
            time.sleep(1.0)

    def moon_units(self, moon, times_s):
        """Interpolated Moon unit vectors at t0+times_s (N,3)."""
        f = (np.asarray(times_s, dtype=float))[:, None] / HORIZON_S
        v = moon["u0"][None, :] * (1 - f) + moon["u1"][None, :] * f
        return v / np.linalg.norm(v, axis=1, keepdims=True)

    def step(self, cfg, observer, moon, now):
        transit_deg = moon["radius_deg"] + cfg["lunar_margin_deg"]
        watch_deg = cfg["lunar_watch_deg"]
        hrz_alt = horizon_alt_at(self.horizon, moon["az"])
        hrz_gate = hrz_alt - cfg.get("horizon_margin_deg", 0.0)
        clear_of_horizon = moon["el"] >= hrz_gate
        moon_up = bool(moon["el"] >= cfg["lunar_min_elev_deg"] and clear_of_horizon)

        aircraft, adsb_age = self.read_aircraft()
        sim = self.sim_aircraft()
        if sim:
            aircraft = aircraft + [sim]

        times = np.arange(0.0, HORIZON_S + STEP_S, STEP_S)
        dt_since_moon = now - moon["t0"]
        moon_u = self.moon_units(moon, times + dt_since_moon)

        results = []
        for a in aircraft:
            lat, lon, alt = project_track(
                a["lat"], a["lon"], a["alt_ft"] * FT_TO_M,
                a["gs_kt"] * KT_TO_MS, a["track"],
                (a["vrate_fpm"] or 0) * FT_TO_M / 60.0, times)
            u = observer.unit_vectors(geodetic_to_ecef(lat, lon, alt))
            el_now = math.degrees(math.asin(max(-1, min(1, u[0, 2]))))
            if el_now < -2:
                continue
            sep = np.degrees(np.arccos(np.clip(np.sum(u * moon_u, axis=1), -1, 1)))
            i_min = int(np.argmin(sep))
            min_sep = float(sep[i_min])
            az_now, el_arr = Observer.azel(u[0:1])
            r = {
                "hex": a["hex"], "flight": a["flight"] or a["hex"],
                "sim": bool(a.get("sim")),
                "az": round(float(az_now[0]), 2), "el": round(el_now, 2),
                "alt_ft": round(a["alt_ft"]), "gs_kt": round(a["gs_kt"]),
                "sep_now": round(float(sep[0]), 3),
                "min_sep": round(min_sep, 3),
                "eta_s": int(times[i_min]),
                "tca_unix": float(now + times[i_min]),
                "transit": bool(min_sep <= transit_deg),
                "watch": bool(min_sep <= watch_deg),
            }
            if min_sep <= PATH_ZONE_DEG:
                # path relative to Moon centre (deg): x = cross-track, y = elevation
                daz = (Observer.azel(u)[0] - Observer.azel(moon_u)[0] + 540) % 360 - 180
                dx = daz * np.cos(np.radians(Observer.azel(u)[1]))
                dy = Observer.azel(u)[1] - Observer.azel(moon_u)[1]
                r["path"] = [[round(float(x), 3), round(float(y), 3)]
                             for x, y in zip(dx, dy)]
            results.append(r)

        results.sort(key=lambda r: r["min_sep"])
        if moon_up:
            self.alert_and_arm(cfg, results, transit_deg, now)
        # drop alert state for aircraft gone from view
        seen = {r["hex"] for r in results}
        for h in list(self.alerted):
            if h not in seen:
                del self.alerted[h]

        with self.lock:
            if moon_up:
                msg = "tracking"
            elif moon["el"] >= cfg["lunar_min_elev_deg"]:
                msg = "moon behind local obstruction (horizon %.0f° at az %.0f°)" % (
                    hrz_gate, moon["az"])
            else:
                msg = "moon below minimum elevation"
            self.snap = {
                "ok": True, "message": msg,
                "now": now,
                "moon": {
                    "az": round(moon["az"], 2), "el": round(moon["el"], 2),
                    "dist_km": round(moon["dist_km"]),
                    "radius_deg": round(moon["radius_deg"], 4),
                    "phase_deg": round(moon["phase"], 1),
                    "illum": round(moon["illum"], 3),
                    "up": moon_up,
                    "min_elev_deg": cfg["lunar_min_elev_deg"],
                    "sun_az": round(moon["sun_az"], 2),
                    "sun_el": round(moon["sun_el"], 2),
                    "horizon_alt": round(hrz_alt, 1),
                },
                "horizon": self.horizon,
                "best_dates": self.best_dates_snapshot(),
                "thresholds": {"transit_deg": round(transit_deg, 3), "watch_deg": watch_deg},
                "adsb_age_s": round(adsb_age, 1) if adsb_age is not None else None,
                "n_tracked": len(results),
                "candidates": results[:25],
                "capture": {
                    "enabled": cfg["capture_enabled"],
                    "host": cfg["capture_host"], "port": cfg["capture_port"],
                    **self.capture.snapshot(now),
                },
                "events": list(self.events)[:40],
                "sim_active": self.sim is not None,
            }

    def alert_and_arm(self, cfg, results, transit_deg, now):
        for r in results:
            st = self.alerted.setdefault(r["hex"], {})
            if r["transit"]:
                self.capture.arm(r["hex"], r["tca_unix"],
                                 cfg["capture_pre_s"], cfg["capture_post_s"])
                if "transit" not in st:
                    st["transit"] = now
                    self.log_event("transit",
                                   f"{r['flight']} predicted transit — min sep "
                                   f"{r['min_sep']:.3f}° in {r['eta_s']}s", **{
                                       k: r[k] for k in ("hex", "min_sep", "eta_s")})
                    self.notify(cfg,
                        f"🌕✈️ <b>LUNAR TRANSIT IMMINENT</b>\n"
                        f"<b>{r['flight']}</b> crosses the Moon in <b>~{r['eta_s']}s</b>\n"
                        f"min sep {r['min_sep']:.3f}° (disc+margin {transit_deg:.3f}°)\n"
                        f"alt {r['alt_ft']:,} ft · {r['gs_kt']} kt · "
                        f"az {r['az']}° el {r['el']}°\n"
                        f"📹 capture {'armed' if cfg['capture_enabled'] else 'DISABLED'}"
                        f"{' [SIM]' if r['sim'] else ''}")
            elif r["watch"] and "watch" not in st and "transit" not in st:
                st["watch"] = now
                self.log_event("watch",
                               f"{r['flight']} near miss — min sep {r['min_sep']:.2f}° "
                               f"in {r['eta_s']}s", hex=r["hex"])

    def moon_at(self, unix):
        """Moon + Sun geometry at an arbitrary time (3D time-travel preview)."""
        if self._eph is None:
            raise RuntimeError("ephemeris not loaded yet")
        cfg = self.cfg()
        obs = Observer(cfg["home_lat"], cfg["home_lon"], cfg["home_alt_m"])
        az, el, dist = self.moon_azel(obs, [unix])
        _, illum = self.moon_illum(unix)
        saz, sel = self.sun_azel(obs, unix)
        return {"az": round(float(az[0]), 2), "el": round(float(el[0]), 2),
                "illum": round(float(illum), 3), "dist_km": round(float(dist[0])),
                "sun_az": round(saz, 2), "sun_el": round(sel, 2), "t": unix}

    # ---- API --------------------------------------------------------------
    def snapshot(self):
        with self.lock:
            return dict(self.snap)


def start(state):
    eng = LunarTransitEngine(state)
    threading.Thread(target=eng.run, daemon=True).start()
    return eng
