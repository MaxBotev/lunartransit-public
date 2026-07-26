#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 MaxBotev
"""
LunarTransit standalone server.

Serves the /lunar predictor page + /adsb3d 3D map, runs the transit-prediction
engine, and ingests ADS-B from a configurable source:

    "adsb_source": "http://raspberrypi.local:8080/api/adsb/raw"  (Pi as antenna)
    "adsb_source": "/path/to/dump1090/aircraft.json"             (local dongle)

The enrichment pipeline (routes, classification, type names) runs regardless of
source. When two hosts share one feed, run capture (the SharpCap trigger) and
Telegram alerts on ONE host only to avoid double recordings / duplicate alerts.

Run:  python lunar_server.py          (uses config.json next to this file)
"""

import json
import os
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone

# Windows populates its root-CA store lazily (browsers trigger it, Python
# doesn't), so fresh installs fail TLS verification for some hosts. Use
# certifi's bundle when available for deterministic HTTPS.
try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()

from flask import Flask, Response, jsonify, request

import math

from route_cache import RouteCache
from classify import classify
import aircraft_types
import lunar_transit
from lunar_page import LUNAR_HTML
from adsb3d_page import ADSB3D_HTML
from notify import TelegramNotifier

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
SPOOL_DIR = os.path.join(BASE, "adsb_spool")
SPOOL_JSON = os.path.join(SPOOL_DIR, "aircraft.json")
TRAIL_MAX = 400
TRAIL_TTL = 120.0

DEFAULTS = {
    # Default site: San Francisco International Airport (SFO). Override in
    # config.json or via the 📍 SITE control in the dashboard.
    "home_lat": 37.6213, "home_lon": -122.3790, "home_alt_m": 4.0,
    "web_port": 8080,
    # Prefer hostnames over IPs so DHCP reassignments don't break the link.
    # adsb_source_fallbacks is tried in order when the primary fails.
    "adsb_source": "http://raspberrypi.local:8080/api/adsb/raw",
    "adsb_source_fallbacks": [],
    "dump1090_path": "",            # set when a local dongle arrives
    "lunar_enabled": True, "lunar_margin_deg": 0.10, "lunar_watch_deg": 2.0,
    "lunar_min_elev_deg": 10.0,
    "lunar_notify": False,          # opt-in; requires Telegram creds below
    "telegram_enabled": False, "telegram_bot_token": "", "telegram_chat_id": "",
    "capture_enabled": False,       # opt-in SharpCap trigger
    "capture_host": "127.0.0.1", "capture_port": 5580,
    "capture_pre_s": 20.0, "capture_post_s": 20.0,
    "horizon_file": "horizon.hrz", "horizon_margin_deg": 10.0,
    "notify_kinds": ["military", "mil_helicopter"],
    "notify_cooldown_s": 3600,
}


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def send_aircraft_alert(cfg, a):
    """Telegram alert for a non-civilian contact (ported from the Pi)."""
    token, chat = cfg.get("telegram_bot_token"), cfg.get("telegram_chat_id")
    if not (token and chat):
        return
    dist = haversine_km(cfg["home_lat"], cfg["home_lon"], a["lat"], a["lon"])
    title = {"military": "🛩 Military aircraft",
             "mil_helicopter": "🚁 Military helicopter"}.get(
        a["kind"], "⚠️ Non-civilian aircraft")
    lines = [
        f"<b>{title} detected</b>",
        a.get("type_full") or a.get("icao_type") or "unknown type",
        f"Callsign: <b>{a.get('flight') or a['hex']}</b>",
        f"Altitude: {a['alt']:,} ft" if a.get("alt") else None,
        f"Speed: {round(a['gs'])} kt" if a.get("gs") else None,
        f"Distance: {dist:.1f} km",
        f"Owner: {a.get('owner')}" if a.get("owner") else None,
        f"Reg: {a.get('registration')}" if a.get("registration") else None,
        f'<a href="https://www.google.com/maps?q={a["lat"]},{a["lon"]}">map</a>'
        f' · <a href="https://globe.adsbexchange.com/?icao={a["hex"]}">track</a>',
    ]
    TelegramNotifier(token, chat).send_async(
        "\n".join(x for x in lines if x))

ROUTE_CACHE = RouteCache()


def load_config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.load(open(CONFIG_PATH)))
    except Exception:
        pass
    return cfg


class State:
    """Slim stand-in for the Pi's State: just what the lunar engine and the
    ADS-B endpoints need (lock + cfg + aircraft snapshot + trails)."""

    def __init__(self, cfg):
        self.lock = threading.Lock()
        self.cfg = cfg
        self.aircraft = []
        self.trails = {}
        self.trail_last = {}
        self.adsb_stats = {}
        self.lunar = None

    def snapshot_adsb(self):
        with self.lock:
            return {
                "aircraft": self.aircraft,
                "stats": dict(self.adsb_stats),
                "home": {"lat": self.cfg["home_lat"], "lon": self.cfg["home_lon"]},
                "mode": "adsb",
                "notify_enabled": False,
                "notify_configured": False,
            }


def _fetch_one(src):
    """Fetch aircraft.json from a single URL or local path."""
    if src.startswith("https"):
        with urllib.request.urlopen(src, timeout=4, context=SSL_CTX) as r:
            return json.loads(r.read())
    if src.startswith("http"):
        with urllib.request.urlopen(src, timeout=4) as r:
            return json.loads(r.read())
    with open(src) as f:                      # local file path (dump1090 dir)
        return json.load(f)


_last_good_source = {"src": None}


def fetch_raw_adsb(cfg):
    """Raw dump1090-format aircraft.json from the configured source.

    Tries adsb_source first, then each entry of adsb_source_fallbacks. This is
    what makes a DHCP setup survive a reboot: list the hostname forms your
    network actually resolves (mDNS "sky.local", bare NetBIOS "sky", and a
    last-resort literal IP) and whichever answers wins. The working source is
    remembered and tried first next time, so the common case is one request.
    """
    candidates = [cfg["adsb_source"]] + list(cfg.get("adsb_source_fallbacks") or [])
    last = _last_good_source["src"]
    if last in candidates:                     # prefer whatever worked before
        candidates.insert(0, candidates.pop(candidates.index(last)))
    errors = []
    for src in candidates:
        if not src:
            continue
        try:
            data = _fetch_one(src)
            if _last_good_source["src"] != src:
                print("[adsb] source: %s" % src, flush=True)
                _last_good_source["src"] = src
            return data
        except Exception as e:
            errors.append("%s: %s" % (src, e))
    raise RuntimeError("no ADS-B source reachable — " + "; ".join(errors))


def reload_config_if_changed(state):
    """Pick up hand-edits to config.json without a restart (checked at 1 Hz).
    Values changed via /api/config already live in memory; this merges disk
    edits (e.g. tuning horizon_margin_deg while watching the sky)."""
    try:
        mt = os.path.getmtime(CONFIG_PATH)
    except OSError:
        return
    if mt == getattr(state, "_cfg_mtime", None):
        return
    try:
        disk = json.load(open(CONFIG_PATH))
    except Exception as e:                    # partially-written file: retry later
        print("[config] unreadable, ignoring: %s" % e, flush=True)
        return
    with state.lock:
        state._cfg_mtime = mt
        changed = {k: v for k, v in disk.items() if state.cfg.get(k) != v}
        state.cfg.update(disk)
    if changed:
        safe = {k: ("<set>" if "token" in k or "chat" in k else v)
                for k, v in changed.items()}
        print("[config] reloaded: %s" % safe, flush=True)


def adsb_worker(state):
    """1 Hz: fetch raw ADS-B, enrich (routes/type/kind), maintain trails, and
    spool the raw JSON locally for the prediction engine."""
    os.makedirs(SPOOL_DIR, exist_ok=True)
    notified = {}    # hex -> monotonic time of last military alert
    while True:
        try:
            reload_config_if_changed(state)
            with state.lock:
                cfg = dict(state.cfg)
            data = fetch_raw_adsb(cfg)
            if cfg["adsb_source"].startswith("http"):
                # spool for the engine only when the source is remote; local
                # files are read directly (avoids Windows rename-lock races)
                tmp = SPOOL_JSON + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(data, f)
                os.replace(tmp, SPOOL_JSON)

            acft = []
            for a in data.get("aircraft", []):
                if a.get("lat") is None or a.get("lon") is None:
                    continue
                flight = (a.get("flight") or "").strip()
                hexid = a.get("hex")
                route = ROUTE_CACHE.route(flight)
                info = ROUTE_CACHE.aircraft(hexid)
                category = a.get("category")
                icao_type = info.get("icao_type") if info else None
                owner = info.get("owner") if info else None
                adsbdb_model = info.get("type") if info else None
                ac_class = aircraft_types.aircraft_class(icao_type)
                engine = aircraft_types.engine_type(icao_type)
                type_full = (aircraft_types.friendly_name(icao_type, adsbdb_model)
                             if icao_type else None)
                type_short = (aircraft_types.friendly_short(icao_type, adsbdb_model)
                              if icao_type else None)
                kind = classify(category=category, hexid=hexid, owner=owner,
                                icao_type=icao_type, ac_class=ac_class, engine=engine)
                acft.append({
                    "hex": hexid, "flight": flight,
                    "lat": a.get("lat"), "lon": a.get("lon"),
                    "alt": a.get("alt_baro"), "gs": a.get("gs"),
                    "track": a.get("track"),
                    "vrate": a.get("baro_rate") or a.get("geom_rate") or 0,
                    "seen": a.get("seen"), "rssi": a.get("rssi"),
                    "category": category, "kind": kind,
                    "icao_type": icao_type, "type_full": type_full,
                    "type_short": type_short, "type_name": adsbdb_model,
                    "registration": info.get("registration") if info else None,
                    "owner": owner,
                    "airline": route.get("airline") if route else None,
                    "origin": route.get("origin") if route else None,
                    "destination": route.get("destination") if route else None,
                })
            acft.sort(key=lambda x: x.get("flight") or x.get("hex") or "")
            now_m = time.monotonic()
            with state.lock:
                for a in acft:
                    h = a["hex"]
                    pt = [round(a["lat"], 5), round(a["lon"], 5)]
                    tr = state.trails.setdefault(h, [])
                    if not tr or tr[-1] != pt:
                        tr.append(pt)
                        if len(tr) > TRAIL_MAX:
                            del tr[:len(tr) - TRAIL_MAX]
                    state.trail_last[h] = now_m
                    a["trail"] = tr
                for h in list(state.trails):
                    if now_m - state.trail_last.get(h, 0) > TRAIL_TTL:
                        state.trails.pop(h, None)
                        state.trail_last.pop(h, None)
                state.aircraft = acft
                state.adsb_stats = {
                    "messages": data.get("messages", 0),
                    "aircraft": len(acft),
                    "total_tracked": len(data.get("aircraft", [])),
                    "last_update": datetime.now(timezone.utc).isoformat(),
                    "source": cfg["adsb_source"][:60],
                }
            # military-contact Telegram alerts (same behavior as the Pi)
            if cfg.get("telegram_enabled") and cfg.get("telegram_bot_token"):
                kinds = set(cfg.get("notify_kinds") or [])
                cooldown = cfg.get("notify_cooldown_s", 3600)
                for a in acft:
                    if (a["kind"] in kinds
                            and now_m - notified.get(a["hex"], -1e9) > cooldown):
                        notified[a["hex"]] = now_m
                        send_aircraft_alert(cfg, a)
                for h in list(notified):
                    if now_m - notified[h] > cooldown * 2:
                        del notified[h]
        except Exception as e:
            with state.lock:
                state.adsb_stats = {"error": str(e), "aircraft": 0}
        time.sleep(1.0)


# ---------------------------------------------------------------------------
def create_app(state):
    app = Flask(__name__)

    @app.route("/")
    def index():
        return Response('<meta http-equiv="refresh" content="0;url=/adsb3d">',
                        mimetype="text/html")

    @app.route("/adsb3d")
    def adsb3d():
        with state.lock:
            lat, lon = state.cfg["home_lat"], state.cfg["home_lon"]
        html = ADSB3D_HTML.replace("__LAT__", str(lat)).replace("__LON__", str(lon))
        return Response(html, mimetype="text/html")

    @app.route("/lunar")
    def lunar_page():
        return Response(LUNAR_HTML, mimetype="text/html")

    @app.route("/api/adsb")
    def api_adsb():
        return jsonify(state.snapshot_adsb())

    @app.route("/api/config", methods=["GET", "POST"])
    def api_config():
        if request.method == "POST":
            data = request.get_json(force=True, silent=True) or {}
            bounds = {"home_lat": (-90, 90), "home_lon": (-180, 180),
                      "home_alt_m": (-430, 9000)}
            with state.lock:
                for k, (lo, hi) in bounds.items():
                    if k in data:
                        try:
                            v = float(data[k])
                        except (TypeError, ValueError):
                            return jsonify({"ok": False, "info": f"bad {k}"})
                        if not lo <= v <= hi:
                            return jsonify({"ok": False, "info": f"{k} out of range"})
                        state.cfg[k] = v
                cfg = dict(state.cfg)
            json.dump(cfg, open(CONFIG_PATH, "w"), indent=2)
        with state.lock:
            c = dict(state.cfg)
        return jsonify({"ok": True, "home_lat": c["home_lat"],
                        "home_lon": c["home_lon"],
                        "home_alt_m": c.get("home_alt_m", 60.0)})

    @app.route("/api/lunar")
    def api_lunar():
        eng = state.lunar
        return jsonify(eng.snapshot() if eng
                       else {"ok": False, "message": "engine not running"})

    @app.route("/api/lunar/at")
    def api_lunar_at():
        try:
            t = float(request.args.get("t", ""))
        except ValueError:
            return jsonify({"ok": False, "info": "bad t (unix seconds)"})
        try:
            return jsonify(state.lunar.moon_at(t))
        except Exception as e:
            return jsonify({"ok": False, "info": str(e)})

    @app.route("/api/lunar/capture-test", methods=["POST"])
    def api_capture_test():
        eng = state.lunar
        c = eng.cfg()
        if not c["capture_host"]:
            return jsonify({"ok": False, "info": "capture_host not configured"})
        ok, info = eng.capture.test(c["capture_host"], c["capture_port"])
        return jsonify({"ok": ok, "info": str(info)})

    @app.route("/api/lunar/capture-manual", methods=["POST"])
    def api_capture_manual():
        """Operator REC/STOP from the dashboard — no prediction involved."""
        eng = state.lunar
        if not eng:
            return jsonify({"ok": False, "info": "engine not running"})
        action = ((request.get_json(force=True, silent=True) or {})
                  .get("action") or "").upper()
        if action not in ("REC", "STOP"):
            return jsonify({"ok": False, "info": "action must be REC or STOP"})
        c = eng.cfg()
        if not c["capture_host"]:
            return jsonify({"ok": False, "info": "capture_host not configured"})
        ok, info = eng.capture.manual(c["capture_host"], c["capture_port"], action,
                                      c.get("manual_capture_max_s", 300.0))
        eng.log_event("capture", "manual %s -> %s:%s (%s)" % (
            action, c["capture_host"], c["capture_port"], info), ok=ok)
        return jsonify({"ok": ok, "info": str(info),
                        "recording": eng.capture.manual_rec})

    @app.route("/api/lunar/simulate", methods=["POST"])
    def api_simulate():
        eng = state.lunar
        snap = eng.snapshot() if eng else None
        if not snap or not snap.get("moon"):
            return jsonify({"ok": False, "info": "moon data not ready"})
        if snap["moon"]["el"] < 5:
            return jsonify({"ok": False,
                            "info": "moon below 5° — geometry impossible to simulate"})
        c = eng.cfg()
        obs = lunar_transit.Observer(c["home_lat"], c["home_lon"], c["home_alt_m"])
        eng.start_simulation(obs, snap["moon"]["az"], snap["moon"]["el"])
        return jsonify({"ok": True})


    _geo_cache = {}

    @app.route("/api/geocode")
    def api_geocode():
        # place-name -> lat/lon via Nominatim (proxied for UA policy + caching)
        import urllib.parse as _up
        q = (request.args.get("q") or "").strip()
        if not q:
            return jsonify([])
        if q.lower() in _geo_cache:
            return jsonify(_geo_cache[q.lower()])
        url = ("https://nominatim.openstreetmap.org/search?format=json&limit=5&q="
               + _up.quote(q))
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "LunarTransit dashboard"})
            with urllib.request.urlopen(req, timeout=10, context=SSL_CTX) as r:
                raw = json.loads(r.read())
        except Exception as e:
            return jsonify({"error": str(e)})
        out = [{"name": x.get("display_name"),
                "lat": float(x["lat"]), "lon": float(x["lon"])} for x in raw]
        _geo_cache[q.lower()] = out
        return jsonify(out)

    _wx_cache = {"t": 0, "data": None, "key": None}

    @app.route("/api/wx")
    def api_wx():
        # METAR cloud layers + visibility for stations in the slab (NOAA AWC),
        # cached 5 min — feeds the dashboard's weather rendering
        try:
            r_km = min(300.0, float(request.args.get("r_km", 130)))
        except ValueError:
            r_km = 130.0
        with state.lock:
            lat, lon = state.cfg["home_lat"], state.cfg["home_lon"]
        key = f"{round(lat, 2)},{round(lon, 2)},{round(r_km)}"
        now = time.time()
        if (_wx_cache["data"] is not None and _wx_cache["key"] == key
                and now - _wx_cache["t"] < 300):
            return jsonify(_wx_cache["data"])
        dlat = r_km / 111.32
        dlon = r_km / (111.32 * math.cos(math.radians(lat)))
        url = ("https://aviationweather.gov/api/data/metar?format=json&bbox="
               f"{lat - dlat:.3f},{lon - dlon:.3f},{lat + dlat:.3f},{lon + dlon:.3f}")
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "LunarTransit dashboard"})
            with urllib.request.urlopen(req, timeout=10, context=SSL_CTX) as r:
                raw = json.loads(r.read())
        except Exception as e:
            return jsonify({"ok": False, "info": str(e), "stations": []})
        out = []
        for s in raw:
            try:
                vis = s.get("visib")
                vis = (float(str(vis).replace("+", ""))
                       if vis is not None else None)
            except ValueError:
                vis = None
            out.append({"id": s.get("icaoId"), "lat": s.get("lat"),
                        "lon": s.get("lon"), "elev_m": s.get("elev") or 0,
                        "obs": s.get("obsTime"),
                        "visib_mi": vis, "fltCat": s.get("fltCat"),
                        "clouds": [c for c in (s.get("clouds") or [])
                                   if isinstance(c.get("base"), (int, float))],
                        "raw": s.get("rawOb")})
        data = {"ok": True, "t": now, "stations": out}
        _wx_cache.update({"t": now, "data": data, "key": key})
        return jsonify(data)

    # ---- tile + moon-texture proxies (same sources as the Pi) ----
    tile_cache = os.path.join(BASE, "terrain_cache")
    tile_sources = {
        "terrain": ("https://s3.amazonaws.com/elevation-tiles-prod/terrarium/"
                    "{z}/{x}/{y}.png", "image/png"),
        "sat": ("https://server.arcgisonline.com/ArcGIS/rest/services/"
                "World_Imagery/MapServer/tile/{z}/{y}/{x}", "image/jpeg"),
        "dark": ("https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png",
                 "image/png"),
        "osm": ("https://tile.openstreetmap.org/{z}/{x}/{y}.png", "image/png"),
    }

    def _proxy(url, path, mime):
        os.makedirs(tile_cache, exist_ok=True)
        if not os.path.exists(path):
            req = urllib.request.Request(
                url, headers={"User-Agent": "LunarTransit dashboard"})
            try:
                with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as r:
                    data = r.read()
                with open(path, "wb") as f:
                    f.write(data)
            except Exception as e:
                return Response(f"fetch failed: {e}", status=502)
        with open(path, "rb") as f:
            return Response(f.read(), mimetype=mime,
                            headers={"Cache-Control": "max-age=86400"})

    @app.route("/api/mtile/<src>/<int:z>/<int:x>/<int:y>")
    def api_mtile(src, z, x, y):
        if src not in tile_sources or not (0 <= z <= 16
                                           and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
            return Response("bad tile", status=400)
        url_tpl, mime = tile_sources[src]
        return _proxy(url_tpl.format(z=z, x=x, y=y),
                      os.path.join(tile_cache, f"{src}_{z}_{x}_{y}"), mime)

    @app.route("/api/moontex")
    def api_moontex():
        return _proxy("https://svs.gsfc.nasa.gov/vis/a000000/a004700/a004720/"
                      "lroc_color_poles_1k.jpg",
                      os.path.join(tile_cache, "moon_lroc_1k.jpg"), "image/jpeg")

    return app


def main():
    cfg = load_config()
    state = State(cfg)
    try:
        state._cfg_mtime = os.path.getmtime(CONFIG_PATH)
    except OSError:
        state._cfg_mtime = None
    src = cfg["adsb_source"]
    lunar_transit.ADSB_JSON = SPOOL_JSON if src.startswith("http") else src
    threading.Thread(target=adsb_worker, args=(state,), daemon=True).start()
    state.lunar = lunar_transit.start(state)
    print(f"LunarTransit server on :{cfg['web_port']} — "
          f"adsb_source={cfg['adsb_source']}")
    create_app(state).run(host="0.0.0.0", port=cfg["web_port"], threaded=True)


if __name__ == "__main__":
    main()
