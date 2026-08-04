# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 MaxBotev
"""
Which telescope is on the mount, and what that means for keeping the Moon.

Everything in meridian_flip and moon_find rests on one assumption: the lunar
disc fits inside the frame, so its edge can be measured. That is what makes the
plate scale self-calibrating, what makes "centred" meaningful, and what lets a
spiral search recognise the Moon when it lands on it.

At 2350 mm that assumption collapses. Measured on the Askar at 1.195 arcsec/px:

                          Askar FRA500      C9.25
    focal length mm                500       2350
    arcsec / pixel               1.195      0.254
    field, deg             1.28 x 0.72  0.27 x 0.15
    Moon vs frame width            41%       191%
    Moon vs frame height           72%       337%

The C9.25 sees about a fifth of the lunar disc's area. There is no edge in the
frame to measure, the lit region is simply "all of it", and every disc-fitting
routine would return the frame's own dimensions and confidently compute a plate
scale four times wrong. A 0.6 degree search step is more than twice the entire
field of view.

So the narrow strategy does not look for a disc at all. It keeps the Moon by
dead reckoning against the ephemeris: ask where the Moon is now, ask where the
mount says it is pointing, and correct the difference. No image is involved,
which is exactly the point -- a frame filled edge to edge with lunar surface
carries no information about where in the disc it sits, and never will.

That makes a good sync a precondition rather than a nicety. After one the mount
is right to about an arcminute, which is 11% of this frame; without one it is
degrees, which is off the Moon entirely.
"""

PROFILES = {
    "askar": {
        "name": "Askar FRA500",
        "focal_mm": 500.0,
        "strategy": "disc",       # the disc fits: measure it and close the loop
        "search_step_deg": 0.6,
        "recentre_tol_arcsec": 60.0,
    },
    "c925": {
        "name": "Celestron 9.25 (no reducer)",
        "focal_mm": 2350.0,
        "strategy": "narrow",     # the Moon overfills: dead-reckon instead
        "search_step_deg": 0.12,  # under half the 0.27 deg field
        "recentre_tol_arcsec": 20.0,
    },
}

DEFAULTS = {
    "optics_profile": "askar",
    # Above this fraction of the frame the disc is treated as overfilling even
    # if the profile says otherwise -- a reducer, a different camera or a
    # cropped ROI all change the arithmetic, and the frame itself is the
    # authority on what actually fits in it.
    "narrow_when_disc_frac": 0.85,
    "narrow_interval_s": 120.0,   # dead reckoning drifts, so correct often
    "narrow_max_step_arcsec": 900.0,
}


def profile(cfg):
    """The active profile, with any per-key overrides from config applied."""
    key = str(cfg.get("optics_profile") or "askar").lower()
    p = dict(PROFILES.get(key) or PROFILES["askar"])
    p["key"] = key if key in PROFILES else "askar"
    for k in ("search_step_deg", "recentre_tol_arcsec"):
        if cfg.get(k) is not None and cfg.get("optics_profile_override"):
            p[k] = cfg[k]
    return p


def is_narrow(cfg, diam_px=None, frame_px=None):
    """Should the narrow strategy be used?

    The profile decides by default, but a measured disc overrules it: if the
    lit region spans most of the frame there is no edge to work with, whatever
    the configuration claims is attached.
    """
    if diam_px and frame_px:
        try:
            if float(diam_px) >= float(cfg.get("narrow_when_disc_frac", 0.85)) \
                    * float(frame_px):
                return True
        except (TypeError, ValueError):
            pass
    return profile(cfg)["strategy"] == "narrow"


def expected_scale(cfg, ref_scale=1.195, ref_focal_mm=494.0):
    """Predicted arcsec/pixel for the active profile.

    Useful before anything has been measured -- for sizing a search step, and
    for sanity-checking a calibration on a night when the disc cannot be used
    as a ruler because it does not fit.
    """
    return ref_scale * ref_focal_mm / profile(cfg)["focal_mm"]
