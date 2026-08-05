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

from meridian_flip import FlipError, Keeper, _kv, _talk


class NarrowKeeper:
    """Keeps a frame-filling Moon in place by dead reckoning."""

    def __init__(self, cfg, engine, log, observer=None):
        self.cfg = cfg
        self.engine = engine
        self.log = log
        self.observer = observer
        self.last = 0.0
        self.busy = False
        self.corrections = 0
        self._warned_region = False
        # Dead reckoning needs no pictures, which is the whole point -- but it
        # means nothing here ever measures the sky, and the exposure loop lives
        # on the image-based keeper. Left as it was, auto_gain would silently do
        # nothing at 2350 mm: enabled in config, wired up, never called. So one
        # frame is grabbed on a slow cadence purely for its brightness, and the
        # existing, tested gain machinery is reused rather than reimplemented.
        self.gain = Keeper(cfg, engine, log)
        self.last_gain = 0.0

    def say(self, text, **kw):
        self.log("narrow", text, **kw)

    def cmd(self, c, timeout=120.0):
        return _talk(self.cfg["capture_host"], self.cfg["capture_port"], c, timeout)

    def should_run(self, now, moon_el, min_elev, capture_active):
        if self.busy or capture_active or moon_el < min_elev:
            return False
        return now - self.last >= float(self.cfg.get("narrow_interval_s", 120.0))

    def check_gain(self, now):
        """One frame, for exposure only. Never for pointing.

        With the disc overfilling the frame there is no sky in it, so `sky` is
        dark lunar terrain rather than background -- but `peak` is still the
        brightest surface in view, and keeping that off the clipping point is
        what the loop is actually for.
        """
        if not self.cfg.get("auto_gain"):
            return
        every = float(self.cfg.get("gain_interval_s", 300.0))
        if now - self.last_gain < every:
            return
        self.last_gain = now
        try:
            reply = self.cmd("MOONPOS", timeout=120.0)
        except FlipError as e:
            self.gain.full = self.cfg
            self.gain.bright_sky_gain(e)      # a blown-out frame still steers it
            return
        self.gain.full = self.cfg
        self.gain.f.last_reply = reply
        self.gain.auto_gain(now)

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
            off_e, off_n, what = self.aim_offset(time.time())
            cosd0 = math.cos(math.radians(max(-89.0, min(89.0, dec))))
            if off_e or off_n:
                # East on the sky is +RA, north is +Dec, so the offset drops
                # straight in. It is recomputed every tick because libration
                # keeps moving a fixed lunar feature across the apparent disc.
                ra += off_e / 3600.0 / 15.0 / max(cosd0, 1e-3)
                dec += off_n / 3600.0

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
            self.say("dead-reckoned %.0f arcsec back onto %s "
                     "(dRA %+.0f dDec %+.0f)" % (err, what, dra, ddec),
                     err_arcsec=round(err), aim=what)
        except Exception as e:
            self.say("correction failed: %s" % e, ok=False)
        finally:
            self.busy = False
        try:
            self.check_gain(now)
        except Exception as e:
            self.say("exposure check failed: %s" % e, ok=False)


    def aim_offset(self, now):
        """Where to point relative to the Moon's centre: (east", north", label).

        With a disc four times the frame, "the Moon" is not a target. Locking
        onto a selenographic coordinate is, and it has to be recomputed
        continuously: libration swings a fixed feature by hundreds of arcsec
        across a night, which at this scale is several frame widths.
        """
        mode = str(self.cfg.get("aim_mode") or "centre").lower()
        if mode == "transit":
            aim = getattr(self.engine, "_transit_aim", None)
            if aim and now < aim["until"]:
                return (aim["east"], aim["north"],
                        "where %s crosses" % aim["flight"])
            return 0.0, 0.0, "the Moon's centre"
        if mode != "region":
            return 0.0, 0.0, "the Moon's centre"
        lat = self.cfg.get("aim_region_lat")
        lon = self.cfg.get("aim_region_lon")
        if lat is None or lon is None or self.observer is None:
            return 0.0, 0.0, "the Moon's centre"
        try:
            import lunar_features
            o = lunar_features.feature_offset(self.engine, self.observer, now,
                                              float(lat), float(lon))
        except Exception as e:
            if not self._warned_region:
                self._warned_region = True
                self.say("cannot compute the lunar region offset (%s) — "
                         "falling back to the disc centre" % e, ok=False)
            return 0.0, 0.0, "the Moon's centre"
        if not o["visible"] and not self._warned_region:
            self._warned_region = True
            self.say("%.2fN %.2fE is on the far side right now (libration puts "
                     "it %.0f%% of a radius out) — aiming at the limb above it, "
                     "which is where an ejecta plume would appear"
                     % (float(lat), float(lon), 100 * o["limb_fraction"]),
                     ok=False)
        # Clamp to the limb: a point round the back projects onto the edge, and
        # pointing further would take the Moon out of frame entirely.
        e_as, n_as = o["d_east_arcsec"], o["d_north_arcsec"]
        r = math.hypot(e_as, n_as)
        lim = o["moon_radius_arcsec"] * float(self.cfg.get("aim_max_radius_frac", 1.0))
        if r > lim and r > 0:
            e_as, n_as = e_as * lim / r, n_as * lim / r
        return e_as, n_as, "%.1fN %.1fE on the Moon" % (float(lat), float(lon))


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
