# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 MaxBotev
"""
Automatic meridian flip with closed-loop centring on the lunar disc.

Why closed loop: a German-equatorial mount's pointing error depends on which
side of the pier it is on, so with imperfect polar alignment (plus cone and
orthogonality error) a blind GOTO back to the same coordinates after a flip
does not land on the same spot -- the error reverses sign across the meridian
and roughly doubles. Plate solving cannot rescue it either, because at lunar
exposures there are no stars in the frame.

So the Moon itself is the reference. The sequence is:

  1. stop any capture, note the current tracking rate
  2. slew across the meridian to where the Moon WILL be on arrival (it moves
     ~33 arcsec/min, so a 90 s flip is ~0.8 arcmin), refusing to proceed unless
     the mount agrees the GOTO changes pier side, and verifying that it did
  3. restore lunar-rate tracking
  4. calibrate: two small probe slews reveal how RA/Dec map onto image axes
     (camera rotation is unknown and changes if the camera is ever rotated)
  5. iterate: snap -> find the disc -> convert the pixel offset to an on-sky
     correction -> nudge -> repeat until inside tolerance
  6. resume

Plate scale is self-calibrating: the Moon's true angular diameter comes from
the ephemeris, so measuring the disc in pixels gives arcsec/pixel directly --
no focal length or pixel size needed, and it stays correct if the ROI changes.
"""

import math
import socket
import threading
import time

DEFAULTS = {
    "auto_flip": False,             # master switch (off until deliberately armed)
    "flip_after_min": 2.0,          # start this long PAST the meridian
    "flip_before_limit_min": 12.0,  # give up beyond this (mount limit ~16-18)
    "flip_slew_s": 90.0,            # expected slew duration, for Moon lead
    "centre_tol_arcsec": 40.0,      # ~2% of the disc; the mount under-
                                # delivers slews ~10%, so one pass leaves
                                # ~20% residual and a second closes it
    "centre_max_iter": 6,
    "calib_step_arcsec": 300.0,     # probe size for the rotation calibration
    "min_illum_for_centring": 0.35, # below this the lit centroid is unreliable
}


class FlipError(Exception):
    pass


def _talk(host, port, cmd, timeout=200.0):
    """One command, one reply, on the same line protocol the capture uses."""
    with socket.create_connection((host, int(port)), timeout=20.0) as s:
        s.sendall((cmd + "\n").encode())
        s.settimeout(timeout)
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
    reply = buf.decode(errors="replace").strip()
    if not reply:
        raise FlipError("no reply to %r" % cmd)
    if reply.startswith("ERR"):
        raise FlipError("%s -> %s" % (cmd.split()[0], reply[4:].strip()))
    return reply


def _kv(reply):
    """Parse 'OK a=1 b=2' into {'a': '1', 'b': '2'}."""
    out = {}
    for tok in reply.split()[1:]:
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k] = v
    return out


def _disc(reply):
    """MOONPOS reply -> (cx, cy, diameter_px, frame_w, frame_h).

    The bounding box's LONGEST side is the true diameter at any phase: the
    terminator only ever cuts the short axis. Its centre is the disc centre
    for a gibbous or full Moon.
    """
    d = _kv(reply)
    x0, y0 = float(d["bx0"]), float(d["by0"])
    x1, y1 = float(d["bx1"]), float(d["by1"])
    diam = max(x1 - x0, y1 - y0)
    if diam <= 0:
        raise FlipError("degenerate disc bounding box")
    return (0.5 * (x0 + x1), 0.5 * (y0 + y1), diam,
            float(d["w"]), float(d["h"]))


class Flipper:
    """Runs one flip. Instantiated per attempt; not reused."""

    def __init__(self, cfg, engine, log):
        self.cfg = dict(DEFAULTS)
        for k in DEFAULTS:
            if k in cfg:
                self.cfg[k] = cfg[k]
        self.host = cfg["capture_host"]
        self.port = cfg["capture_port"]
        self.engine = engine          # for Moon ephemeris
        self.log = log                # log_event(kind, text, **extra)
        self.scale = None             # arcsec per pixel
        self.rot = None               # (a, b, c, d): pixel delta -> (dRA, dDec)

    # ---- helpers ---------------------------------------------------------
    def say(self, text, **kw):
        self.log("flip", text, **kw)

    def cmd(self, c, timeout=200.0):
        return _talk(self.host, self.port, c, timeout)

    def moon_radec(self, when):
        """Apparent topocentric RA (hours) / Dec (deg) at a unix time."""
        return self.engine.moon_radec_at(when)

    def measure(self):
        """Snap and locate the disc. Returns (cx, cy, diam_px, w, h)."""
        return _disc(self.cmd("MOONPOS", timeout=120.0))

    def offset_arcsec(self, m):
        """Pixel offset of the disc from frame centre -> arcsec, using the
        Moon's known angular diameter as the ruler."""
        cx, cy, diam_px, w, h = m
        ang_diam = self.engine.moon_angular_diameter_arcsec()
        self.scale = ang_diam / diam_px
        return (cx - w / 2.0) * self.scale, (cy - h / 2.0) * self.scale

    # ---- rotation calibration -------------------------------------------
    def calibrate(self):
        """Two probe slews to learn how (dRA, dDec) move the disc in pixels.

        Solves the 2x2 the other way round so we can go straight from a
        measured pixel error to the correction that cancels it.
        """
        step = float(self.cfg["calib_step_arcsec"])
        base = self.measure()
        bx, by = base[0], base[1]

        self.cmd("NUDGE %.1f 0" % step)
        m = self.measure()
        dx_ra, dy_ra = (m[0] - bx) / step, (m[1] - by) / step
        self.cmd("NUDGE %.1f 0" % (-step))

        self.cmd("NUDGE 0 %.1f" % step)
        m = self.measure()
        dx_dec, dy_dec = (m[0] - bx) / step, (m[1] - by) / step
        self.cmd("NUDGE 0 %.1f" % (-step))

        det = dx_ra * dy_dec - dx_dec * dy_ra
        if abs(det) < 1e-9:
            # Two very different faults look identical here, so say which.
            same = (abs(dx_ra) + abs(dy_ra) + abs(dx_dec) + abs(dy_dec)) < 1e-9
            raise FlipError(
                ("the camera image did not change at all across two %.0f-arcsec "
                 "probe slews — either the frame is stale (SharpCap returned the "
                 "same image twice) or the mount ignored the slews"
                 % step) if same else
                ("calibration degenerate: probe slews moved the disc along one "
                 "axis only (det=%.2e)" % det))
        # invert [[dx_ra, dx_dec], [dy_ra, dy_dec]]
        self.rot = (dy_dec / det, -dx_dec / det, -dy_ra / det, dx_ra / det)
        self.say("centring calibrated: 1 arcsec RA = (%.3f,%.3f) px, "
                 "1 arcsec Dec = (%.3f,%.3f) px" % (dx_ra, dy_ra, dx_dec, dy_dec))

    def pixel_to_correction(self, dx_px, dy_px):
        """Pixel error -> (dRA, dDec) arcsec that cancels it."""
        a, b, c, d = self.rot
        return -(a * dx_px + b * dy_px), -(c * dx_px + d * dy_px)

    # ---- centring --------------------------------------------------------
    def centre(self):
        tol = float(self.cfg["centre_tol_arcsec"])
        for i in range(int(self.cfg["centre_max_iter"])):
            m = self.measure()
            ex, ey = self.offset_arcsec(m)
            err = math.hypot(ex, ey)
            if err <= tol:
                self.say("centred: %.0f arcsec from frame centre "
                         "(%.2f arcsec/px)" % (err, self.scale))
                return err
            if self.rot is None:
                self.calibrate()
            dx_px = m[0] - m[3] / 2.0
            dy_px = m[1] - m[4] / 2.0
            dra, ddec = self.pixel_to_correction(dx_px, dy_px)
            self.say("centring %d/%d: off by %.0f arcsec -> nudge "
                     "dRA=%.0f dDec=%.0f" % (i + 1, self.cfg["centre_max_iter"],
                                             err, dra, ddec))
            self.cmd("NUDGE %.1f %.1f" % (dra, ddec))
        raise FlipError("did not converge after %d centring iterations"
                        % self.cfg["centre_max_iter"])

    # ---- the whole sequence ---------------------------------------------
    def run(self):
        illum = self.engine.moon_illum_now()
        if illum < float(self.cfg["min_illum_for_centring"]):
            raise FlipError("Moon only %.0f%% lit — the lit centroid is not a "
                            "reliable disc centre; flip skipped" % (100 * illum))

        st = _kv(self.cmd("MOUNT"))
        # prefer the listener's decoded W/E; drivers spell the raw enum in at
        # least three different ways (West / pierWest / ThroughThePole)
        pier_before = st.get("side") or st.get("pier", "?")
        if pier_before == "?":
            raise FlipError("mount reports an unknown pier side (%s) — a flip "
                            "cannot be verified from here" % st.get("pier"))
        tracking = str(st.get("tracking", "")).lower() == "true"
        self.say("flip starting (pier %s, HA %+.2f h, tracking=%s)" % (
            pier_before, float(st.get("ha", 0.0)), st.get("tracking")))
        if not tracking:
            # Not fatal: some mounts are configured to stop tracking AT the
            # meridian, so arriving here with motors already stopped is normal.
            # The Moon has been drifting since (~15 arcsec/s), but the slew
            # below targets its ephemeris position, so it gets re-acquired.
            self.say("note: mount was NOT tracking when the flip began — it "
                     "likely hit its own meridian limit already; re-acquiring "
                     "the Moon from the ephemeris")

        try:
            self.cmd("STOP", timeout=30.0)     # never flip mid-recording
        except FlipError:
            pass

        target_t = time.time() + float(self.cfg["flip_slew_s"])
        ra, dec = self.moon_radec(target_t)
        self.say("slewing to the Moon's position in %.0fs: RA %.5f h Dec %.4f deg"
                 % (self.cfg["flip_slew_s"], ra, dec))
        reply = self.cmd("FLIP %.6f %.6f" % (ra, dec))
        self.say(reply[3:].strip())

        st = _kv(self.cmd("MOUNT"))
        if (st.get("side") or st.get("pier")) == pier_before:
            raise FlipError("pier side unchanged (%s) — flip did not happen"
                            % pier_before)

        # The mount has already moved by this point, so a rate problem must not
        # abort the sequence — being centred at sidereal rate beats being left
        # uncentred. Tracking being OFF is fatal, though: nothing else works.
        try:
            self.say(self.cmd("TRACK ON LUNAR")[3:].strip())
        except FlipError as e:
            self.say("WARNING: could not set lunar rate (%s) — continuing" % e)
            self.cmd("TRACK ON")     # tracking itself is non-negotiable
        err = self.centre()

        st = _kv(self.cmd("MOUNT"))
        pier_after = st.get("side") or st.get("pier")
        self.say("flip complete: pier %s -> %s, centred to %.0f arcsec, "
                 "tracking=%s rate=%s" % (pier_before, pier_after, err,
                                          st.get("tracking"), st.get("rate")),
                 pier_from=pier_before, pier_to=pier_after,
                 residual_arcsec=round(err, 1))
        return True


def start(cfg, engine, log, on_done=None):
    """Run a flip on a worker thread so the prediction loop keeps running."""
    def _worker():
        try:
            Flipper(cfg, engine, log).run()
            ok = True
        except Exception as e:
            log("flip", "AUTOMATIC FLIP FAILED: %s — mount left as-is, "
                        "take manual control" % e, ok=False)
            ok = False
        if on_done:
            on_done(ok)
    t = threading.Thread(target=_worker, name="meridian-flip", daemon=True)
    t.start()
    return t
