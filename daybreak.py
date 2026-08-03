# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 MaxBotev
"""
Put the rig to bed at dawn, before the Sun gets to it.

The Moon sets, the session is over, and within a couple of hours the Sun is
high enough to cook whatever the telescope is pointing at. The job is to stop
tracking, park, and close the flat panel's cover -- and only then, optionally,
power the machines down.

The ordering is the whole design, and it runs strictly one way:

    stop capture -> park the mount -> CLOSE THE COVER -> verify it closed
                 -> shut down Windows -> shut down the Pi

Every step is verified before the next begins, and any failure ABORTS the rest
and leaves everything powered up. That rule exists because the failure it
prevents is unrecoverable: if the cover does not close and the sequence
continues anyway, the optics are pointed at a rising Sun with no machine left
running to do anything about it. A rig that stays up with a stuck cover is a
phone call; a rig that shuts down with a stuck cover is a repair bill.

For the same reason the two shutdowns are separately switchable and both
default to off, and the Pi always goes last -- it is the only thing that can
still act once Windows is gone.
"""

import subprocess
import threading
import time

from meridian_flip import FlipError, _talk

DEFAULTS = {
    "auto_daybreak": False,          # master switch
    "daybreak_below_elev_deg": 10.0,  # Moon this low means the session is done
    "daybreak_grace_s": 600.0,       # ...and has stayed there this long
    "daybreak_park": True,
    "daybreak_close_cover": True,
    # Both off by default. Powering machines down is not reversible from here.
    "daybreak_shutdown_windows": False,
    "daybreak_shutdown_pi": False,
    "daybreak_windows_user": "",     # for the ssh shutdown; empty = skip
    "daybreak_windows_host": "",
    # Deep Sky Dad registers six numbered CoverCalibrator instances, so the
    # listener cannot autodetect. Held here rather than in the listener because
    # the listener's global resets every time the script is re-run.
    "cover_progid": "",
}


class Daybreak:
    """Runs the end-of-session sequence once per night."""

    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self.done = False
        self.below_since = None
        self.busy = False

    def say(self, text, **kw):
        self.log("daybreak", text, **kw)

    def cmd(self, c, timeout=200.0):
        return _talk(self.cfg["capture_host"], self.cfg["capture_port"], c, timeout)

    # ---- trigger ---------------------------------------------------------
    def should_run(self, now, moon_el):
        """Only once, only when the Moon has been down for a while.

        The grace period matters: elevation crosses the threshold once per
        night, but a single sample near the boundary -- or a restart -- should
        not trigger a shutdown on its own.
        """
        if self.done or self.busy or not self.cfg.get("auto_daybreak"):
            return False
        floor = float(self.cfg.get("daybreak_below_elev_deg", 10.0))
        if moon_el >= floor:
            self.below_since = None
            return False
        if self.below_since is None:
            self.below_since = now
            return False
        return now - self.below_since >= float(self.cfg.get("daybreak_grace_s", 600.0))

    # ---- the sequence ----------------------------------------------------
    def run(self, moon_el):
        self.busy = True
        try:
            self._run(moon_el)
        except Exception as e:
            self.say("DAYBREAK ABORTED: %s — everything left powered and as-is, "
                     "check the rig" % e, ok=False)
        finally:
            self.busy = False

    def _run(self, moon_el):
        self.say("Moon is down (%.1f deg) — putting the rig to bed" % moon_el)

        # 1. never leave a recording running
        try:
            self.cmd("STOP", timeout=30.0)
        except FlipError as e:
            self.say("could not stop the capture (%s) — continuing" % e, ok=False)

        # 2. park. A mount left tracking walks the OTA across the sky all day.
        if self.cfg.get("daybreak_park", True):
            self.say(self.cmd("PARK", timeout=240.0)[3:].strip())

        # 3. the cover. This is the step the whole sequence exists for.
        closed = not self.cfg.get("daybreak_close_cover", True)
        if not closed:
            progid = self.cfg.get("cover_progid") or ""
            if progid:
                self.cmd("COVER PROGID %s" % progid, timeout=30.0)
            # The command's return value is NOT the thing being decided on --
            # the hardware state is. Observed live: a COVER CLOSE that really
            # did close the cover returned "Access to the port COM3 is denied",
            # because Windows had not finished releasing the handle from the
            # previous call. Treating that reply as failure would abort a
            # sequence that had actually succeeded.
            try:
                self.say(self.cmd("COVER CLOSE", timeout=180.0)[3:].strip())
            except FlipError as e:
                self.say("close reported '%s' — checking what the cover "
                         "actually did before deciding" % e, ok=False)

            state = "not read"
            for i in range(6):
                time.sleep(3.0 if i else 1.0)
                try:
                    state = self.cmd("COVER STATE", timeout=60.0)
                except FlipError as e:
                    state = "unreadable (%s)" % e
                    continue
                if "Closed" in state:
                    closed = True
                    break
            if not closed:
                raise FlipError(
                    "the cover is not closed after 6 checks (%s). NOT shutting "
                    "anything down: with the optics open and the Sun coming up, "
                    "the machines staying on is the only way you can still fix "
                    "this remotely" % state.replace("OK ", ""))
            self.say("cover confirmed closed")

        # 4/5. power down, Windows first -- the Pi is the only thing that can
        # still act once Windows is gone, so it goes last.
        if self.cfg.get("daybreak_shutdown_windows"):
            self._shutdown_windows()
        if self.cfg.get("daybreak_shutdown_pi"):
            self.say("shutting down the Pi — goodnight")
            time.sleep(3.0)                   # let the log and Telegram flush
            subprocess.Popen(["sudo", "shutdown", "-h", "now"])
        self.done = True

    def _shutdown_windows(self):
        user = self.cfg.get("daybreak_windows_user") or ""
        host = self.cfg.get("daybreak_windows_host") or self.cfg["capture_host"]
        if not user:
            self.say("Windows shutdown is enabled but daybreak_windows_user is "
                     "not set — skipping it", ok=False)
            return
        try:
            r = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                 "%s@%s" % (user, host), "shutdown /s /t 20 /c \"LunarTransit "
                 "daybreak\""],
                capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                self.say("Windows is shutting down in 20 s")
            else:
                self.say("Windows shutdown command failed: %s"
                         % (r.stderr or r.stdout).strip()[:200], ok=False)
        except Exception as e:
            self.say("could not reach Windows to shut it down: %s" % e, ok=False)


def start(cfg, log, moon_el, holder):
    """Run the sequence on a worker thread so the prediction loop keeps going."""
    t = threading.Thread(target=holder.run, args=(moon_el,),
                         name="daybreak", daemon=True)
    t.start()
    return t
