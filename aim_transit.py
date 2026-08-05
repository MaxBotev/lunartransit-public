# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 MaxBotev
"""
Point at the part of the Moon the aircraft will actually cross.

On the Askar this was never a question: the whole disc sat inside the frame, so
"aim at the Moon" and "aim at the crossing" were the same instruction. At
2350 mm the frame is 0.27 x 0.15 degrees against a disc 0.52 degrees across,
and the two come apart badly. Measured against 62 predicted transits from this
system's own log:

    median separation from disc centre     0.181 deg
    inside the C9.25 half-height (0.077)      24%
    inside the C9.25 half-width  (0.135)      40%

So a scope parked on the disc centre records the right patch of Moon about a
quarter of the time, and the aircraft crosses a limb nobody is looking at for
the rest. The predictor already knows where the crossing happens; it just was
not being used for anything except deciding whether to record.

The conversion avoids parallactic angles entirely. Rather than rotating an
alt-azimuth offset into equatorial coordinates -- which is easy to get subtly
backwards and impossible to notice until a night is wasted -- the aircraft's
own apparent RA/Dec at closest approach is computed directly from its azimuth
and elevation, and differenced against the Moon's. Two absolute positions,
one subtraction, no convention to get wrong.
"""

import math

FT_TO_M = 0.3048
KT_TO_MS = 0.514444


def azel_to_radec(az_deg, el_deg, lat_deg, lst_hours):
    """Horizontal -> equatorial, for one instant at one site.

    Standard spherical trigonometry rather than a library call: the inputs are
    already topocentric apparent az/el from the same pipeline that produced the
    prediction, so re-deriving them through a different path would introduce a
    discrepancy rather than remove one.
    """
    a = math.radians(el_deg)
    A = math.radians(az_deg)          # from north, through east
    phi = math.radians(lat_deg)

    sin_dec = (math.sin(a) * math.sin(phi)
               + math.cos(a) * math.cos(phi) * math.cos(A))
    sin_dec = max(-1.0, min(1.0, sin_dec))
    dec = math.asin(sin_dec)

    cos_dec = math.cos(dec)
    if abs(cos_dec) < 1e-9:                     # at the pole, RA is arbitrary
        return lst_hours % 24.0, math.degrees(dec)
    sin_H = -math.sin(A) * math.cos(a) / cos_dec
    cos_H = (math.sin(a) - math.sin(dec) * math.sin(phi)) / (cos_dec * math.cos(phi))
    H = math.atan2(sin_H, cos_H)                # hour angle, radians
    ra = (lst_hours - math.degrees(H) / 15.0) % 24.0
    return ra, math.degrees(dec)


def crossing_offset(engine, observer, result, cfg):
    """Sky offset of the predicted crossing from the Moon's centre, in arcsec.

    Returns (east, north, info) or None when it cannot be computed. East is
    +RA, north is +Dec -- the same convention the narrow keeper's aim offset
    uses, so the two are interchangeable.
    """
    import numpy as np
    from datetime import datetime, timezone
    from lunar_transit import (Observer, geodetic_to_ecef, project_track)

    tca = float(result.get("tca_unix") or 0.0)
    if tca <= 0:
        return None
    ac = result.get("_ac")
    if not ac:
        return None

    # Where the aircraft is at closest approach, from its own track
    lag = float(ac.get("lag_s") or 0.0)
    dt = tca - float(result.get("_now") or tca)
    lat, lon, alt = project_track(
        ac["lat"], ac["lon"], ac["alt_ft"] * FT_TO_M,
        ac["gs_kt"] * KT_TO_MS, ac["track"],
        (ac.get("vrate_fpm") or 0) * FT_TO_M / 60.0,
        np.array([dt + lag]))
    u, _rng = observer.unit_and_range(geodetic_to_ecef(lat, lon, alt))
    az, el = Observer.azel(u)
    az, el = float(az[0]), float(el[0])

    # BOTH positions are derived the same way, from az/el through the same
    # transform, and only then differenced. That matters: moon_radec_at returns
    # J2000 (Skyfield's radec() default) while an az/el conversion is of-date,
    # and 26 years of precession between them is about 1300 arcsec -- twenty
    # times the whole frame. Differencing two positions in one frame cancels it
    # exactly, and the residual from applying an of-date OFFSET to a J2000
    # centre is the offset rotated by the precession angle, under 6 arcsec at
    # a lunar radius.
    t = engine._ts.from_datetime(datetime.fromtimestamp(tca, tz=timezone.utc))
    lst = (t.gmst + observer.lon / 15.0) % 24.0
    ac_ra, ac_dec = azel_to_radec(az, el, observer.lat, lst)

    m_az, m_el, _d = engine.moon_azel(observer, np.array([tca]))
    m_ra, m_dec = azel_to_radec(float(m_az[0]), float(m_el[0]),
                                observer.lat, lst)

    cosd = math.cos(math.radians(m_dec))
    d_east = ((ac_ra - m_ra + 12.0) % 24.0 - 12.0) * 15.0 * 3600.0 * cosd
    d_north = (ac_dec - m_dec) * 3600.0

    # Never aim off the disc. A prediction whose closest approach is outside
    # the Moon is a near miss, not a transit, and chasing it would swing the
    # frame onto empty sky just as the recording starts.
    r = math.hypot(d_east, d_north)
    try:
        lim = 0.5 * engine.moon_angular_diameter_arcsec()
    except Exception:
        lim = 1000.0
    lim *= float(cfg.get("aim_max_radius_frac", 1.0))
    clamped = False
    if r > lim and r > 0:
        d_east, d_north = d_east * lim / r, d_north * lim / r
        clamped = True

    return d_east, d_north, {
        "flight": result.get("flight"),
        "tca_unix": tca,
        "offset_arcsec": round(r, 1),
        "offset_frac_of_radius": round(r / max(lim, 1.0), 3),
        "clamped": clamped,
        "az": round(az, 2), "el": round(el, 2),
    }
