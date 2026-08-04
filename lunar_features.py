# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 MaxBotev
"""
Where a named place on the Moon appears in the sky, right now.

Pointing at "the Moon" is not enough once the disc overfills the frame. A C9.25
sees a fifth of it, so the question becomes which fifth -- and for a target like
an impact site that is a fixed selenographic coordinate, not a fixed offset.
The apparent position of a lunar feature moves as libration rocks the Moon by
up to about eight degrees each way, which near the limb is the difference
between a feature being visible and being round the back.

The chain is: selenographic latitude/longitude -> a vector in the Moon's own
body-fixed frame -> rotate into the sky frame using the lunar orientation
kernel -> project onto the plane of sky as an offset from the disc centre.
Skyfield does the hard part; the kernels are the DE421 lunar orientation PCK
and the Moon body frame definition.

Foreshortening is reported alongside, because a feature at the very limb is
compressed to nothing and its apparent offset is almost the full lunar radius
no matter how far round the back it actually sits. A point can be "visible" and
still be a hopeless target.
"""

import math
import os

BASE = os.path.dirname(os.path.abspath(__file__))
MOON_RADIUS_KM = 1737.4

_FRAME = [None]           # built once; the kernels are a few MB


def _frame():
    if _FRAME[0] is None:
        from skyfield.api import load_file
        from skyfield.planetarylib import PlanetaryConstants
        pc = PlanetaryConstants()
        pc.read_text(open(os.path.join(BASE, "moon_080317.tf"), "rb"))
        for name in ("pck00011.tpc", "pck00010.tpc", "pck00008.tpc"):
            p = os.path.join(BASE, name)
            if os.path.exists(p):
                pc.read_text(open(p, "rb"))
                break
        # The handle must stay open: jplephem reads segments lazily, so a
        # closed file surfaces much later as "seek of closed file".
        _FRAME.append(open(os.path.join(BASE, "moon_pa_de421_1900-2050.bpc"), "rb"))
        pc.read_binary(_FRAME[-1])
        _FRAME[0] = pc.build_frame_named("MOON_ME_DE421")
    return _FRAME[0]


def feature_offset(engine, observer, unix_time, sel_lat_deg, sel_lon_deg):
    """Apparent offset of a selenographic point from the Moon's disc centre.

    Returns arcsec east/north on the sky, whether the point is on the visible
    hemisphere, and how foreshortened it is (1.0 face-on, 0.0 exactly at the
    limb).

    Selenographic longitude is positive EAST, which is the IAU convention the
    impact predictions use -- so 93.293 W is passed as -93.293.
    """
    from datetime import datetime, timezone
    from skyfield.api import wgs84
    import numpy as np

    ts = engine._ts
    t = ts.from_datetime(datetime.fromtimestamp(unix_time, tz=timezone.utc))
    site = engine._earth + wgs84.latlon(observer.lat, observer.lon,
                                        elevation_m=observer.alt_m)

    # Where the Moon's centre is, and how far away
    astrometric = site.at(t).observe(engine._moon).apparent()
    dist_km = astrometric.distance().km

    # The feature as a vector in the Moon's body-fixed frame, then in the sky
    lat, lon = math.radians(sel_lat_deg), math.radians(sel_lon_deg)
    body = np.array([math.cos(lat) * math.cos(lon),
                     math.cos(lat) * math.sin(lon),
                     math.sin(lat)]) * MOON_RADIUS_KM
    R = _frame().rotation_at(t)          # body-fixed -> ICRF
    icrf = R.T @ body if R.shape == (3, 3) else np.einsum("ij...,j->i...", R, body)

    # Sky basis at the Moon's centre: east and north unit vectors
    moon_vec = astrometric.position.km
    r = moon_vec / np.linalg.norm(moon_vec)
    zhat = np.array([0.0, 0.0, 1.0])
    east = np.cross(zhat, r)
    east /= np.linalg.norm(east)
    north = np.cross(r, east)

    d_east = float(np.dot(icrf, east))
    d_north = float(np.dot(icrf, north))
    d_los = float(np.dot(icrf, r))       # +ve = away from us, i.e. far side

    # Radians -> arcsec at the Moon's distance
    k = 206264.806 / dist_km
    rho = math.hypot(d_east, d_north)
    return {
        "d_east_arcsec": d_east * k,
        "d_north_arcsec": d_north * k,
        "visible": d_los < 0.0,
        "foreshorten": max(0.0, min(1.0, abs(d_los) / MOON_RADIUS_KM))
                       if d_los < 0 else 0.0,
        "limb_fraction": rho / MOON_RADIUS_KM,     # 1.0 = exactly at the limb
        "moon_radius_arcsec": MOON_RADIUS_KM * k,
        "dist_km": dist_km,
    }


def libration(engine, observer, unix_time):
    """Sub-observer selenographic point -- the Moon's libration right now.

    Computed directly rather than searched: it is the point where the line to
    the observer pierces the surface, which is one rotation of one vector. A
    feature is visible when its longitude lies within about 90 degrees of this,
    so it is the number that decides whether a limb target is in view tonight.
    """
    from datetime import datetime, timezone
    from skyfield.api import wgs84
    import numpy as np

    t = engine._ts.from_datetime(
        datetime.fromtimestamp(unix_time, tz=timezone.utc))
    site = engine._earth + wgs84.latlon(observer.lat, observer.lon,
                                        elevation_m=observer.alt_m)
    moon_vec = site.at(t).observe(engine._moon).apparent().position.km
    towards_us = -np.asarray(moon_vec, dtype=float)      # Moon centre -> us
    body = _frame().rotation_at(t).dot(towards_us)
    n = float(np.linalg.norm(body))
    return {
        "sub_lat_deg": math.degrees(math.asin(float(body[2]) / n)),
        "sub_lon_deg": math.degrees(math.atan2(float(body[1]), float(body[0]))),
    }
