# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 MaxBotev
"""
Score a candidate observing site from recorded traffic, and find the good ones.

The idea is simple even though the arithmetic is not: a transit happens when an
aircraft's line of sight from the observer lands on the Moon. The recorded
histogram says where aircraft are, in absolute coordinates; the ephemeris says
where the Moon will be, for any site and time. Put a candidate site between the
two and count the coincidences.

What makes this worth doing is that the answer changes a lot over small
distances. An airliner at 12 km is 30 degrees up; walk a kilometre and its
bearing shifts by several degrees -- far more than the Moon's half-degree disc.
So a site two miles down the road really is a different proposition, and the
only way to know is to compute it.

The estimate is a RATE -- expected crossings per hour of usable Moon -- not a
prediction of particular flights. Traffic varies, ADS-B drops out, and the
histogram is binned at roughly one lunar diameter. Treat a site scoring twice
another as genuinely better; treat 8.1 versus 8.4 as a tie.

Nothing here runs during a session. It is heavy, it is called from the web page,
and it reads a database that a different thread appends to.
"""

import math
import time

import numpy as np

import traffic_db

EARTH_R = 6378137.0
FT = 0.3048


def _ecef(lat_deg, lon_deg, alt_m):
    """Geodetic -> ECEF, vectorised. Same WGS-84 as the prediction engine."""
    f = 1.0 / 298.257223563
    e2 = f * (2.0 - f)
    lat = np.radians(lat_deg)
    lon = np.radians(lon_deg)
    s, c = np.sin(lat), np.cos(lat)
    n = EARTH_R / np.sqrt(1.0 - e2 * s * s)
    return np.stack([(n + alt_m) * c * np.cos(lon),
                     (n + alt_m) * c * np.sin(lon),
                     (n * (1.0 - e2) + alt_m) * s], axis=-1)


def _enu_basis(lat_deg, lon_deg):
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    sl, cl = math.sin(lat), math.cos(lat)
    so, co = math.sin(lon), math.cos(lon)
    return np.array([[-so, co, 0.0],
                     [-sl * co, -sl * so, cl],
                     [cl * co, cl * so, sl]])


def bins_to_azel(rows, site_lat, site_lon, site_alt_m, bin_deg, bin_ft):
    """Azimuth/elevation of every recorded bin, as seen from one site.

    This is the step that makes the whole thing site-specific, and the reason
    the database stores positions rather than the angles the engine already had.
    """
    a = np.asarray(rows, dtype=np.float64)
    lat = (a[:, 0] + 0.5) * bin_deg
    lon = (a[:, 1] + 0.5) * bin_deg
    alt = (a[:, 2] + 0.5) * bin_ft * FT
    tod = a[:, 3].astype(np.int64)
    n = a[:, 4]

    p = _ecef(lat, lon, alt) - _ecef(np.array([site_lat]), np.array([site_lon]),
                                     np.array([site_alt_m]))[0]
    enu = p @ _enu_basis(site_lat, site_lon).T
    e, nn, u = enu[:, 0], enu[:, 1], enu[:, 2]
    rng = np.sqrt(e * e + nn * nn + u * u)
    az = np.degrees(np.arctan2(e, nn)) % 360.0
    el = np.degrees(np.arcsin(np.clip(u / np.maximum(rng, 1.0), -1.0, 1.0)))
    return az, el, rng, tod, n


def moon_track(engine, observer_cls, lat, lon, alt_m, t0, days, step_s,
               min_elev, hours_per_bucket):
    """Where the Moon will be from this site: (az, el, tod) samples.

    Sampled rather than integrated because the Moon's path is what it is -- the
    point is to walk along it and see what traffic sits on it.
    """
    obs = observer_cls(lat, lon, alt_m)
    times = np.arange(t0, t0 + days * 86400.0, step_s)
    az, el, _dist = engine.moon_azel(obs, times)
    keep = el >= min_elev
    if not np.any(keep):
        return np.array([]), np.array([]), np.array([]), 0.0
    az, el, times = az[keep], el[keep], times[keep]
    hours = np.array([time.localtime(t).tm_hour for t in times])
    tod = (hours // max(1, int(hours_per_bucket))).astype(np.int64)
    up_hours = len(times) * step_s / 3600.0
    return az, el, tod, up_hours


def score_site(engine, observer_cls, lat, lon, alt_m, cfg, days=30,
               step_s=120.0, min_elev=15.0, radius_deg=0.35, rows=None):
    """Expected transits per hour of usable Moon, for one candidate site.

    radius_deg is the Moon's radius plus a little: a crossing counts if the
    aircraft's bearing falls inside the disc.
    """
    bin_deg = float(cfg.get("traffic_bin_deg", 0.002))
    bin_ft = float(cfg.get("traffic_bin_ft", 500.0))
    hpb = int(cfg.get("traffic_hours_per_bucket", 3))
    if rows is None:
        rows = traffic_db.load_bins()
    if not rows:
        return {"ok": False, "info": "no traffic recorded yet"}

    az, el, rng, tod, n = bins_to_azel(rows, lat, lon, alt_m, bin_deg, bin_ft)
    maz, mel, mtod, up_hours = moon_track(engine, observer_cls, lat, lon, alt_m,
                                          time.time(), days, step_s, min_elev,
                                          hpb)
    if not len(maz) or up_hours <= 0:
        return {"ok": False, "info": "the Moon does not rise above %.0f deg "
                                     "here in the next %d days" % (min_elev, days)}

    # Walk the Moon's path and collect the traffic sitting on it. Comparing in
    # az/el needs the cos(el) squeeze on azimuth, or every target near the
    # zenith looks like a match.
    total = 0.0
    hits = np.zeros(len(az), dtype=np.float64)
    for i in range(len(maz)):
        same = tod == mtod[i]
        if not np.any(same):
            continue
        daz = (az - maz[i] + 180.0) % 360.0 - 180.0
        sep = np.hypot(daz * math.cos(math.radians(mel[i])), el - mel[i])
        m = same & (sep <= radius_deg)
        if np.any(m):
            total += float(np.sum(n[m]))
            hits[m] += n[m]

    # A bin's count is samples, one per engine tick, so it is dwell time rather
    # than a number of aircraft. Dividing by the tick rate turns it back into
    # aircraft-seconds on the Moon, and by the crossing time into crossings.
    tick_hz = 1.0
    cross_s = float(cfg.get("typical_crossing_s", 1.2))
    crossings = total / tick_hz / cross_s
    per_hour = crossings / up_hours * (step_s / 60.0) / 60.0

    order = np.argsort(hits)[::-1][:12]
    return {
        "ok": True,
        "lat": round(lat, 5), "lon": round(lon, 5),
        "score_per_hour": round(per_hour, 3),
        "moon_hours": round(up_hours, 1),
        "bins_on_path": int(np.count_nonzero(hits)),
        "bins_total": len(az),
        "days": days, "min_elev_deg": min_elev,
        "top_directions": [
            {"az": round(float(az[i]), 1), "el": round(float(el[i]), 1),
             "range_km": round(float(rng[i]) / 1000.0, 1),
             "weight": int(hits[i])}
            for i in order if hits[i] > 0],
    }


def hotspots(engine, observer_cls, lat, lon, alt_m, cfg, half_deg=0.12,
             n_side=9, **kw):
    """Score a grid of sites around a centre, for a heat map.

    Deliberately coarse and bounded: this is an on-demand web request, and the
    useful output is "north-east of here is better", not a survey.
    """
    rows = traffic_db.load_bins()
    if not rows:
        return {"ok": False, "info": "no traffic recorded yet"}
    out = []
    best = None
    coslat = math.cos(math.radians(lat))
    for i in range(n_side):
        for j in range(n_side):
            dlat = -half_deg + 2.0 * half_deg * i / max(1, n_side - 1)
            dlon = (-half_deg + 2.0 * half_deg * j / max(1, n_side - 1)) / coslat
            r = score_site(engine, observer_cls, lat + dlat, lon + dlon, alt_m,
                           cfg, rows=rows, **kw)
            if not r.get("ok"):
                continue
            cell = {"lat": r["lat"], "lon": r["lon"],
                    "score": r["score_per_hour"]}
            out.append(cell)
            if best is None or cell["score"] > best["score"]:
                best = cell
    if not out:
        return {"ok": False, "info": "nothing scoreable in that area"}
    scores = [c["score"] for c in out]
    return {"ok": True, "cells": out, "best": best,
            "centre": {"lat": round(lat, 5), "lon": round(lon, 5)},
            "min": min(scores), "max": max(scores),
            "half_deg": half_deg, "n_side": n_side}
