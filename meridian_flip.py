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

import json
import math
import re
import os
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
    # A sync corrects the pointing model for the pier side it was taken on, so
    # it does NOT survive a flip: the polar-misalignment component reverses.
    # Re-syncing once the Moon is centred on the NEW side corrects that side
    # too, so both are good for the rest of the night.
    "sync_after_flip": True,
    # --- unattended keeper -------------------------------------------------
    # An equatorial tracking at lunar rate corrects RA only, so the Moon's
    # declination motion (~15 arcsec/min) is never tracked and the target walks
    # out of frame. Re-centring on a timer costs one frame every few minutes
    # and is far cheaper than continuous feature tracking.
    "auto_recentre": False,
    "recentre_interval_s": 300.0,
    "recentre_tol_arcsec": 60.0,
    "stop_tracking_below_min_elev": False,   # mount's own altitude limit is
                                             # the real safety; this is extra
}


class FlipError(Exception):
    pass


def _looks_empty(exc):
    """True when the complaint is 'I cannot see the disc' rather than a fault.

    Kept here rather than in moon_find so the flip can use it without the two
    modules importing each other at load time.
    """
    m = str(exc).lower()
    return any(s in m for s in ("no disc", "lit samples", "no samples"))


# --- remembered pointing bias ---------------------------------------------
# The mount's model error is not random: measured on two different nights it
# was 2.743 deg and 2.697 deg, and the two vectors differed by only 0.175 deg
# -- a fifth of the camera's short axis. That is cone/home-offset error, and it
# comes back the same after every re-home. Remembering it turns a fifty-tile
# spiral search into a single pointing.
#
# Stored PER PIER SIDE: the polar-misalignment part of the error reverses
# across the meridian, so a bias measured on one side is wrong on the other.
BIAS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "pointing_bias.json")


MIN_BIAS_DEG = 0.5


def save_bias(side, dra_deg, ddec_deg, log=None):
    """Record how far the mount's model was off, on-sky degrees, for `side`.

    A near-zero error is NOT recorded. What this file is for is the COLD error
    a re-home restores; once a sync has corrected the model, the measured error
    is ~0, and saving that would overwrite the very number the next search
    needs. Observed live: a search run minutes after a good sync measured
    0.07 deg and replaced a hard-won 2.70 deg hint with it.
    """
    if not side or side == "?":
        return
    if math.hypot(dra_deg, ddec_deg) < MIN_BIAS_DEG:
        if log:
            log("find", "model is already corrected (%.2f deg) — keeping the "
                        "existing pointing bias for the next re-home"
                        % math.hypot(dra_deg, ddec_deg))
        return
    try:
        data = {}
        if os.path.exists(BIAS_PATH):
            with open(BIAS_PATH) as f:
                data = json.load(f)
        data[side] = {"dra_deg": round(float(dra_deg), 4),
                      "ddec_deg": round(float(ddec_deg), 4),
                      "t": time.time()}
        tmp = BIAS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.rename(tmp, BIAS_PATH)          # never leave a half-written file
        if log:
            log("find", "remembered pointing bias for pier %s: %+.2f, %+.2f deg"
                        % (side, dra_deg, ddec_deg))
    except Exception:
        pass                                # a lost hint must never break a run


def load_bias(side, max_age_days=30.0):
    """The remembered bias for `side`, or None. Stale entries are ignored:
    re-balancing or re-seating the OTA changes the cone error."""
    try:
        with open(BIAS_PATH) as f:
            d = json.load(f)[side]
        if time.time() - float(d.get("t", 0)) > max_age_days * 86400.0:
            return None
        return float(d["dra_deg"]), float(d["ddec_deg"])
    except Exception:
        return None


def _connect(host, port, tries=3):
    """Open the command socket, riding out a transient name-lookup failure.

    mDNS (.local) resolution fails intermittently on this LAN even when the
    host is up: a single blip once ended a Moon search after five pointings.
    A blip lasts well under a second, so a couple of retries costs nothing and
    turns a fatal error into a hiccup.
    """
    last = None
    for i in range(tries):
        try:
            try:
                from lunar_transit import connect_host
                return connect_host(host, port, 20.0)
            except ImportError:
                return socket.create_connection((host, int(port)), timeout=20.0)
        except OSError as e:
            last = e
            if i < tries - 1:
                time.sleep(0.8 * (i + 1))
    raise last


def _talk(host, port, cmd, timeout=200.0):
    """One command, one reply, on the same line protocol the capture uses."""
    conn = _connect(host, port)
    with conn as s:
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
        self.full = cfg               # unmerged config, for anything we hand off to
        self.host = cfg["capture_host"]
        self.port = cfg["capture_port"]
        self.engine = engine          # for Moon ephemeris
        self.log = log                # log_event(kind, text, **extra)
        self.last_reply = None        # raw MOONPOS text (carries sky/peak)
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
        """Snap and locate the disc. Returns (cx, cy, diam_px, w, h).

        The raw reply is kept: it also carries the frame's sky and peak levels,
        which is free brightness telemetry for the exposure loop.
        """
        self.last_reply = self.cmd("MOONPOS", timeout=120.0)
        return _disc(self.last_reply)

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
        # Centring assumes the disc is already somewhere in the frame. After a
        # flip that assumption is weakest: the pointing error reverses sign
        # across the meridian, so the miss on the new side can be degrees where
        # the field is under one. On 2026-08-02 at 04:02 the flip itself worked
        # perfectly -- pier side changed, lunar tracking restored -- and then
        # centring failed on its very first frame because the Moon was nowhere
        # in it. The rig then sat mispointed for two and a half hours and fired
        # eight captures at empty sky, one of them a 0.000 deg transit.
        #
        # An empty frame here is not a failure, it is the expected case. Search
        # for the Moon instead of giving up: the search ends in a sync, which
        # corrects this pier side for the rest of the night.
        try:
            err = self.centre()
        except FlipError as e:
            if not _looks_empty(e):
                raise
            self.say("the Moon is not in the frame after the flip (expected — "
                     "the pointing error reverses across the meridian); "
                     "searching for it")
            import moon_find                    # deferred: moon_find imports us
            out = moon_find.Finder(self.full, self.engine, self.log).run()
            self.say("flip recovered by search: %d pointings, centred to %s "
                     "arcsec" % (out.get("tiles", 0), out.get("residual_arcsec")),
                     recovered_by_search=True)
            return True                         # the search already synced

        # The sync that fixed the old pier side does not carry over -- the
        # polar-misalignment error reverses across the meridian. Now that the
        # Moon is genuinely centred on the NEW side, sync again so this side is
        # corrected too and later GOTOs here land straight away.
        if self.cfg.get("sync_after_flip", True):
            try:
                ra_now, dec_now = self.moon_radec(time.time())
                self.say(self.cmd("SYNC %.6f %.6f" % (ra_now, dec_now),
                                  timeout=60.0)[3:].strip())
            except FlipError as e:
                self.say("WARNING: post-flip sync failed (%s) — pointing on "
                         "this pier side stays uncorrected" % e)

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


def sync_to_moon(cfg, engine, log, max_offset_arcmin=8.0):
    """Correct the mount's pointing model using the Moon as the reference.

    A sync is only meaningful if the scope really is on the target, so this
    MEASURES that first: it locates the lunar disc in the camera and refuses
    unless the disc centre is close to the frame centre. Syncing while the Moon
    is off-frame would teach the mount a brand-new, larger error.

    Returns a dict describing what happened.
    """
    host, port = cfg["capture_host"], cfg["capture_port"]

    # 1. where the Moon actually is, right now
    ra, dec = engine.moon_radec_at(time.time())

    # 2. where the camera is actually looking
    m = _disc(_talk(host, port, "MOONPOS", timeout=120.0))
    cx, cy, diam_px, w, h = m
    scale = engine.moon_angular_diameter_arcsec() / diam_px      # arcsec/px
    dx = (cx - w / 2.0) * scale
    dy = (cy - h / 2.0) * scale
    off_arcmin = math.hypot(dx, dy) / 60.0
    if off_arcmin > max_offset_arcmin:
        raise FlipError(
            "Moon is %.1f arcmin from frame centre (limit %.1f) — centre it "
            "first, or the sync would just teach the mount a new error"
            % (off_arcmin, max_offset_arcmin))

    # 3. what the mount currently believes, so the fix can be reported
    st = _kv(_talk(host, port, "MOUNT"))
    was_ra, was_dec = float(st["ra"]), float(st["dec"])
    cosd = math.cos(math.radians(dec))
    bias_ra, bias_dec = (was_ra - ra) * 15.0 * cosd, was_dec - dec
    err = math.hypot(bias_ra, bias_dec)

    reply = _talk(host, port, "SYNC %.6f %.6f" % (ra, dec), timeout=60.0)
    # Remember it: this same offset comes back after the next re-home, and it
    # is what lets a search start where the Moon actually is.
    save_bias(st.get("side") or "?", bias_ra, bias_dec, log)
    log("sync", "mount synced on the Moon: model was off %.3f deg "
                "(Moon %.1f arcmin from frame centre) — %s"
                % (err, off_arcmin, reply[3:].strip()),
        model_error_deg=round(err, 3), centre_offset_arcmin=round(off_arcmin, 2))
    return {"ok": True, "model_error_deg": round(err, 3),
            "centre_offset_arcmin": round(off_arcmin, 2),
            "moon_ra_h": round(ra, 6), "moon_dec_deg": round(dec, 4),
            "info": reply[3:].strip()}


# --- "is the rig busy doing something more important?" ---------------------
# Nudging the mount is always the lowest-priority job on this rig. A transit
# lasts under a second and cannot be repeated; an autofocus run is minutes of
# the operator's time. Both must win over re-centring.
_FOCUS_SEEN = {}          # host -> (position, when that position was first seen)


def rig_busy(host, port, settle_s=30.0, timeout=30.0):
    """Reason the rig must not be moved right now, or None.

    Autofocus is the awkward one: SharpCap steps the focuser and pauses to
    evaluate each step, so IsMoving reads False in the gaps and a single poll
    happily reports "idle" in the middle of a run. Requiring the position to
    have been STABLE for a while catches the whole run rather than one step of
    it. It also catches a manual focus tweak, which is just as disruptive.
    """
    try:
        st = _kv(_talk(host, port, "CAPST", timeout=timeout))
    except Exception as e:
        return "cannot reach the capture PC (%s)" % e
    if str(st.get("capturing", "")).lower() == "true":
        return "SharpCap is capturing"
    if str(st.get("moving", "")).lower() == "true":
        return "the focuser is moving (autofocus?)"

    pos = st.get("pos")
    if pos in (None, "None", ""):
        return None                      # no focuser: nothing more to check
    now = time.time()
    prev = _FOCUS_SEEN.get(host)
    if prev is None:
        # First sighting establishes a baseline. It is NOT evidence of motion:
        # treating it as one blocked the first re-centre after every restart.
        _FOCUS_SEEN[host] = (pos, None)
        return None
    last_pos, moved_at = prev
    if last_pos != pos:
        _FOCUS_SEEN[host] = (pos, now)
        return "the focuser just moved (%s -> %s) — letting it settle" % (last_pos, pos)
    if moved_at is not None and now - moved_at < settle_s:
        return ("the focuser moved %.0fs ago — waiting %.0fs for the run to finish"
                % (now - moved_at, settle_s))
    return None


class Keeper:
    """Keeps the Moon centred, and stands the rig down when it sets.

    Deliberately periodic rather than continuous: the Moon drifts about
    35 arcsec/min, so a check every few minutes holds it to a few percent of
    the frame, for one captured frame per interval instead of every frame.

    The altitude cut-off here is a convenience, NOT the safety mechanism. Set
    the mount's own Altitude Limit as well -- that keeps working when the Pi,
    the network, or SharpCap does not.
    """

    def __init__(self, cfg, engine, log):
        self.cfg = dict(DEFAULTS)
        for k in DEFAULTS:
            if k in cfg:
                self.cfg[k] = cfg[k]
        self.full = cfg
        self.engine = engine
        self.log = log
        self.f = Flipper(cfg, engine, log)   # carries the cached rotation
        self.last = 0.0
        self.stood_down = False
        self.busy = False
        # Framing state. Without this the keeper reported "no disc" every five
        # minutes for four hours -- 56 identical lines -- while eight transit
        # captures fired at an empty field, one of them a dead-centre 0.000 deg
        # pass. Losing the Moon is an event, not a status line.
        self.last_seen = 0.0        # when the disc was last actually measured
        self.lost_since = None
        self.lost_reason = None
        # exposure loop state
        self._gain_ctl = None
        self._gain_lo = 0.0
        self._gain_hi = 0.0
        self._gain_ok = True
        self._gain_pending = None   # peak before the last change, for verification
        self._gain_deaf = 0

    def _say(self, text, **kw):
        self.log("keeper", text, **kw)

    def should_run(self, now, moon_el, min_elev, capture_active):
        if not self.full.get("auto_recentre"):
            return False
        if self.busy or capture_active:      # never nudge mid-recording
            return False
        return now - self.last >= float(self.cfg["recentre_interval_s"])

    def stand_down(self, moon_el, min_elev):
        """Moon has set below the working elevation: stop cleanly."""
        if self.stood_down:
            return
        self.stood_down = True
        host, port = self.full["capture_host"], self.full["capture_port"]
        try:
            _talk(host, port, "STOP", timeout=30.0)
        except Exception:
            pass
        note = ""
        if self.full.get("stop_tracking_below_min_elev"):
            try:
                _talk(host, port, "TRACK OFF", timeout=30.0)
                note = ", tracking stopped"
            except Exception as e:
                note = ", could NOT stop tracking (%s)" % e
        self._say("Moon below %.0f deg (now %.1f) — capture stopped%s"
                  % (min_elev, moon_el, note), ok=True)

    def tick(self, now, moon_el, min_elev):
        """One re-centre pass. Runs on its own thread; may take seconds."""
        self.busy = True
        self.last = now
        try:
            why = rig_busy(self.full["capture_host"], self.full["capture_port"],
                           float(self.full.get("focus_settle_s", 30.0)))
            if why:
                self._say("re-centre skipped: %s" % why)
                return
            m = self.f.measure()
            ex, ey = self.f.offset_arcsec(m)
            err = math.hypot(ex, ey)
            tol = float(self.cfg["recentre_tol_arcsec"])
            # Drift is error divided by the time it took to accumulate, which
            # is NOT the configured interval: passes get skipped for captures,
            # autofocus and searches, so the real gap is often longer. Using
            # the nominal 5 min reported "104.5 arcsec/min" for an 18-minute
            # gap whose true rate was 29 -- a number that looks alarming and
            # means nothing.
            gap_min = ((now - self.last_seen) / 60.0) if self.last_seen else 0.0
            self._seen(now)
            self.auto_gain(now)
            if err <= tol:
                self._say("check: %.0f arcsec off centre — inside %.0f, left alone"
                          % (err, tol))
                return
            if self.f.rot is None:
                self.f.calibrate()
            dx = m[0] - m[3] / 2.0
            dy = m[1] - m[4] / 2.0
            dra, ddec = self.f.pixel_to_correction(dx, dy)
            self.f.cmd("NUDGE %.1f %.1f" % (dra, ddec))
            m2 = self.f.measure()
            ex2, ey2 = self.f.offset_arcsec(m2)
            rate = ("drift %.1f arcsec/min over %.1f min"
                    % (err / gap_min, gap_min)) if gap_min >= 0.5 else "first pass"
            self._say("re-centred: %.0f -> %.0f arcsec (%s)"
                      % (err, math.hypot(ex2, ey2), rate),
                      before_arcsec=round(err), after_arcsec=round(math.hypot(ex2, ey2)),
                      gap_min=round(gap_min, 1))
        except Exception as e:
            self._on_miss(now, e)
        finally:
            self.busy = False

    # ---- framing state ---------------------------------------------------
    def _seen(self, now):
        """The disc was measured: clear any loss, announcing the recovery."""
        if self.lost_since is not None:
            self._say("Moon is back in the frame after %.0f min"
                      % ((now - self.lost_since) / 60.0))
        self.last_seen = now
        self.lost_since = None
        self.lost_reason = None

    @staticmethod
    def _why_blind(msg):
        """Distinguish 'looking the wrong way' from 'cannot see anything'.

        A bright sky and an empty field produce the same "no disc" text, but
        they need opposite responses: a search fixes mispointing and is futile
        against dawn or thick cloud, where the mount is very likely still
        tracking the Moon correctly.
        """
        m = str(msg)
        got = re.search(r"sky=(\d+) peak=(\d+)", m)
        if got:
            sky, peak = int(got.group(1)), int(got.group(2))
            if sky > 25:
                return ("the sky is too bright to see the disc (sky=%d peak=%d)"
                        " — dawn or cloud, not pointing" % (sky, peak))
            return "the Moon is not in the frame (sky=%d peak=%d)" % (sky, peak)
        return None

    def _on_miss(self, now, exc):
        """One failed pass. Log the transition, not every repetition."""
        blind = self._why_blind(exc)
        if blind is None:                      # a genuine fault, always report
            self._say("re-centre failed: %s" % exc, ok=False)
            return
        if self.lost_since is None:
            self.lost_since = now
            self.lost_reason = blind
            seen = ("last seen %.0f min ago" % ((now - self.last_seen) / 60.0)
                    if self.last_seen else "not seen since startup")
            self._say("LOST THE MOON: %s (%s). Still tracking — if this is a "
                      "tree or a roof it will come back on its own."
                      % (blind, seen), ok=False)
        elif blind != self.lost_reason:
            self.lost_reason = blind           # e.g. dark field -> bright sky
            self._say("still no Moon: %s" % blind, ok=False)

    # ---- exposure ---------------------------------------------------------
    # As dawn comes up the sky level climbs into the Moon's own brightness and
    # the disc stops standing out. Measured on 2026-08-02, five minutes apart:
    # sky 3 -> 23 -> 56 in half an hour. Every keeper frame already reports sky
    # and peak, so holding the disc at a sensible level costs no extra frames.
    #
    # The loop is closed and self-checking rather than calculated: gain is not
    # linear in brightness on most cameras (often 0.1 dB steps), and the frame
    # being measured is a PNG SharpCap wrote, which may carry a display stretch
    # that hides the change entirely. So it takes one small step, then verifies
    # on the next pass that the peak actually moved -- and switches itself off,
    # loudly, if it did not.
    def _read_ctrl(self, want):
        """One camera control as a dict, or None. CTRLS returns blocks of
        semicolon-separated key=value, pipe-separated between controls."""
        try:
            reply = _talk(self.full["capture_host"], self.full["capture_port"],
                          "CTRLS " + want, timeout=30.0)
        except Exception:
            return None
        for block in reply[3:].split("|"):
            got = dict(p.split("=", 1) for p in block.strip().split(";")
                       if "=" in p)
            if want.lower() in got.get("name", "").lower():
                return got
        return None

    def _gain_name(self):
        if self._gain_ctl is not None:
            return self._gain_ctl
        try:
            reply = _talk(self.full["capture_host"], self.full["capture_port"],
                          "CTRLS gain", timeout=30.0)
        except Exception as e:
            self._say("cannot read the camera controls (%s) — exposure loop off"
                      % e, ok=False)
            self._gain_ok = False
            return None
        for block in reply[3:].split("|"):
            got = dict(p.split("=", 1) for p in block.strip().split(";")
                       if "=" in p)
            if "gain" in got.get("name", "").lower():
                self._gain_ctl = got["name"]
                self._gain_lo = float(got.get("MinValue") or 0.0)
                self._gain_hi = float(got.get("MaximumValue")
                                      or got.get("MaxValue") or 0.0)
                if self._gain_hi <= self._gain_lo:
                    self._say("gain control '%s' reports no usable range "
                              "(%s..%s) — exposure loop off"
                              % (self._gain_ctl, self._gain_lo, self._gain_hi),
                              ok=False)
                    self._gain_ok = False
                    return None
                self._say("exposure loop will drive '%s' (%g..%g)"
                          % (self._gain_ctl, self._gain_lo, self._gain_hi))
                return self._gain_ctl
        self._say("this camera exposes no gain control — exposure loop off",
                  ok=False)
        self._gain_ok = False
        return None

    @property
    def last_reply(self):
        return self.f.last_reply

    def auto_gain(self, now):
        """Nudge gain to hold the lunar disc in a sensible brightness band."""
        if not (self.full.get("auto_gain") and self._gain_ok):
            return
        if not self.last_reply:
            return
        d = _kv(self.last_reply)
        try:
            peak, sky = float(d["peak"]), float(d["sky"])
        except Exception:
            return
        lo = float(self.full.get("gain_target_lo", 170.0))
        hi = float(self.full.get("gain_target_hi", 235.0))

        # Did the LAST adjustment actually do anything?
        if self._gain_pending is not None:
            moved = peak - self._gain_pending
            if abs(moved) < 2.0:
                self._gain_deaf += 1
                if self._gain_deaf >= 3:
                    self._gain_ok = False
                    self._say("exposure loop disabled: three gain changes moved "
                              "the measured peak by less than 2 counts. The "
                              "snapshot is probably display-stretched, so its "
                              "levels do not track gain and cannot steer it.",
                              ok=False)
                    return
            else:
                self._gain_deaf = 0
            self._gain_pending = None

        if lo <= peak <= hi:
            return
        name = self._gain_name()
        if not name:
            return
        span = max(1.0, self._gain_hi - self._gain_lo)
        step = span * float(self.full.get("gain_step_frac", 0.05))
        got = self._read_ctrl(name)
        if got is None or "Value" not in got:
            self._say("could not read the current gain", ok=False)
            return
        cur = float(got["Value"])
        want = cur - step if peak > hi else cur + step
        want = max(self._gain_lo, min(self._gain_hi, want))
        if abs(want - cur) < 1e-6:
            self._say("disc peak %d is outside %d-%d but gain is already at "
                      "its %s — adjust exposure time instead"
                      % (peak, lo, hi, "minimum" if peak > hi else "maximum"))
            return
        try:
            reply = _talk(self.full["capture_host"], self.full["capture_port"],
                          "CTRL %s %g" % (name, want), timeout=30.0)
        except Exception as e:
            self._say("gain change refused: %s" % e, ok=False)
            return
        self._gain_pending = peak
        self._say("sky %d, disc peak %d %s the %d-%d band — %s"
                  % (sky, peak, "above" if peak > hi else "below", lo, hi,
                     reply[3:].strip()), sky=int(sky), peak=int(peak))

    def lost_for(self, now):
        """Seconds the Moon has been missing, or 0 if it is in the frame."""
        return 0.0 if self.lost_since is None else now - self.lost_since

    def obstructed(self):
        """True when the miss looks like brightness rather than mispointing."""
        return bool(self.lost_reason and "too bright" in self.lost_reason)


def start_keeper(cfg, engine, log):
    return Keeper(cfg, engine, log)
