# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 MaxBotev
"""
Holding the Moon when it is bigger than the frame.

At 2350 mm the lunar disc is nearly twice the frame's width and over three
times its height, so a frame is a patch of lunar surface with no edge in it.
Nothing can be measured from that: not the disc's centre, not the plate scale,
not which way it has drifted. Every closed-loop routine in this project needs
an edge, and there isn't one.

What there is instead is an ephemeris that already knows exactly where the Moon
is, and a mount whose encoders already know where it is pointing. The
difference between those two is the correction, and neither term needs a
picture. That is the whole method:

    ask the ephemeris where the Moon is now
    ask the mount where it thinks it is
    nudge the difference

It is open loop with respect to the image and closed loop with respect to the
mount, which inverts the usual arrangement -- and at this focal length that is
the right way round, because the image has stopped being informative while the
encoders have not.

The cost is that it inherits the pointing model's error. After a sync that is
about an arcminute, or 11% of this frame; without one it is degrees, which is
not on the Moon at all. So a sync is a precondition here, not an optimisation.
"""

import math
import time

from meridian_flip import FlipError, _kv, _talk


class NarrowKeeper:
    """Keeps a frame-filling Moon in place by dead reckoning."""

    def __init__(self, cfg, engine, log):
        self.cfg = cfg
        self.engine = engine
        self.log = log
        self.last = 0.0
        self.busy = False
        self.corrections = 0

    def say(self, text, **kw):
        self.log("narrow", text, **kw)

    def cmd(self, c, timeout=120.0):
        return _talk(self.cfg["capture_host"], self.cfg["capture_port"], c, timeout)

    def should_run(self, now, moon_el, min_elev, capture_active):
        if self.busy or capture_active or moon_el < min_elev:
            return False
        return now - self.last >= float(self.cfg.get("narrow_interval_s", 120.0))

    def tick(self, now):
        """One correction. Costs two round trips and no frames at all."""
        self.busy = True
        self.last = now
        try:
            st = _kv(self.cmd("MOUNT", timeout=30.0))
            if str(st.get("tracking", "")).lower() != "true":
                self.say("mount is not tracking — nothing to correct")
                return
            mra, mdec = float(st["ra"]), float(st["dec"])
            ra, dec = self.engine.moon_radec_at(time.time())

            cosd = math.cos(math.radians(max(-89.0, min(89.0, dec))))
            dra = (ra - mra) * 15.0 * 3600.0 * cosd     # arcsec on sky
            ddec = (dec - mdec) * 3600.0
            err = math.hypot(dra, ddec)

            tol = float(self.cfg.get("recentre_tol_arcsec", 20.0))
            if err <= tol:
                self.say("on target: %.0f arcsec from the ephemeris position, "
                         "inside %.0f — left alone" % (err, tol))
                return

            # A large correction means the model is wrong, not that the Moon
            # moved: it travels 35 arcsec a minute, so a two-minute interval
            # can only justify about 70. Anything much larger is a bad sync or
            # a mount that has been moved, and slewing on it would make a small
            # framing error into a lost target.
            cap = float(self.cfg.get("narrow_max_step_arcsec", 900.0))
            if err > cap:
                self.say("the mount is %.0f arcsec from where the Moon is, which "
                         "is too far to be drift (limit %.0f) — not correcting. "
                         "Re-sync on the Moon, or check the mount has not been "
                         "moved." % (err, cap), ok=False)
                return

            self.cmd("NUDGE %.1f %.1f" % (dra, ddec))
            self.corrections += 1
            self.say("dead-reckoned %.0f arcsec back onto the ephemeris "
                     "(dRA %+.0f dDec %+.0f)" % (err, dra, ddec),
                     err_arcsec=round(err))
        except Exception as e:
            self.say("correction failed: %s" % e, ok=False)
        finally:
            self.busy = False


def frame_state(reply, cfg):
    """What a MOONPOS reply means when the Moon overfills the frame.

    The bounding box is useless here -- it is the frame -- but the LIT FRACTION
    is not, and MOONPOS already reports everything needed to compute it. Fully
    lit means somewhere on the disc with no edge in view; partly lit means the
    limb is crossing the frame, which is the one case where the image still
    says which way the disc centre lies; dark means off the Moon entirely.
    """
    d = _kv(reply)
    try:
        w, h = float(d["w"]), float(d["h"])
        step = max(1.0, float(d.get("step", 1)))
        n = float(d["n"])
    except (KeyError, TypeError, ValueError):
        return None
    total = max(1.0, (w / step) * (h / step))
    frac = max(0.0, min(1.0, n / total))
    if frac >= 0.97:
        where = "inside the disc, no limb in view"
    elif frac >= 0.03:
        where = "limb crossing the frame"
    else:
        where = "off the Moon"
    return {"lit_fraction": round(frac, 3), "where": where,
            "cx": float(d.get("cx", w / 2)), "cy": float(d.get("cy", h / 2)),
            "w": w, "h": h}
