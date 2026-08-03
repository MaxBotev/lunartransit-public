# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 MaxBotev
# SharpCap Pro capture-trigger listener.
#
# Run this inside SharpCap Pro:  Scripting > Show Console  >  open & run this file
# (or add it to SharpCap's startup scripts). It listens on TCP port 5580 for
# commands from the LunarTransit predictor on the Pi:
#
#   REC\n   -> start a SER video capture on the selected camera
#   STOP\n  -> stop the capture
#   PING\n  -> reply "PONG" (used by the dashboard's TEST LINK button)
#
# Requirements before arming:
#   * camera connected and selected in SharpCap (IMX585, full res or ROI)
#   * output format set to SER, exposure/gain preset for the Moon
#   * mount tracking the Moon at lunar rate
#
# SharpCap's scripting console is IronPython — this file sticks to the 2.x
# stdlib subset that works there.

import math
import socket
import sys
import threading
import time
import traceback

# Bumped whenever a command is added or changed. PING reports it, so "is the
# running listener actually the file on disk?" is one round trip instead of a
# guess -- a stale listener has silently wasted three debugging cycles here,
# each time looking exactly like a code bug.
LISTENER_VERSION = "2026-08-03a"

PORT = 5580
MAX_CAPTURE_S = 120     # safety: force-stop if STOP never arrives
SLEW_TIMEOUT_S = 180    # abort a slew that never finishes
MAX_NUDGE_ARCSEC = 5400 # refuse absurd corrections (1.5 deg) from a bad centroid
SNAP_PATH = r"C:\LunarTransit\af_frame.png"


def _mount():
    """The selected mount's raw ASCOM interface (documented semantics:
    RA in hours, Dec in degrees, SideOfPier as PierSide enum)."""
    m = SharpCap.Mounts.SelectedMount  # noqa: F821
    if m is None:
        raise RuntimeError("no mount selected in SharpCap")
    if not m.Connected:
        raise RuntimeError("mount not connected")
    a = getattr(m, "AscomMount", None)
    if a is None:
        raise RuntimeError("mount is not an ASCOM device")
    return a


def _pier(a):
    try:
        return str(a.SideOfPier)
    except Exception:
        return "?"


def _pier_norm(s):
    """Normalise a pier-side string to 'W' / 'E' / '?'.

    Drivers spell the PierSide enum three different ways and all show up in
    the wild, so match on meaning rather than on any one spelling:

      ASCOM constants     pierWest / pierEast
      plain side names    West / East / Unknown
      POINTING STATE      ThroughThePole / Normal   <-- the ZWO AM5N does this

    The last one is what the ASCOM spec actually says the values mean:
    pierEast is the "normal pointing state" and pierWest the "through the pole
    pointing state". Naming the members after the state rather than the side
    is legal, and an exact string compare against "pierWest" silently never
    matches -- which would make every flip unverifiable.
    """
    s = (s or "").strip().lower()
    if not s:
        return "?"
    if s in ("1", "pierwest"):
        return "W"
    if s in ("0", "piereast"):
        return "E"
    if "west" in s:
        return "W"
    if "east" in s:
        return "E"
    if "pole" in s:                 # ThroughThePole == pierWest
        return "W"
    if "normal" in s:               # Normal == pierEast
        return "E"
    return "?"


def _dest_pier(a, ra):
    """Which side a fresh GOTO to `ra` should land on, and how we know.

    Prefer the driver. Many mounts -- including this one -- do not implement
    DestinationSideOfPier at all, so fall back to the geometry: a German
    equatorial sits WEST of the pier looking east while the target is east of
    the meridian (HA < 0), and EAST of the pier once it is west of the
    meridian (HA > 0). Confirmed against this mount's own log.
    """
    try:
        return _pier_norm(str(a.DestinationSideOfPier(ra, a.Declination))), "driver"
    except Exception:
        pass
    ha = (a.SiderealTime - ra + 12.0) % 24.0 - 12.0
    return ("W" if ha < 0 else "E"), "hour-angle(%.3fh)" % ha


def mount_state():
    """MOUNT — one line of everything the orchestrator needs to decide."""
    try:
        a = _mount()
        raw = _pier(a)
        return ("OK ra=%.6f dec=%.6f pier=%s side=%s ha=%.5f lst=%.6f tracking=%s "
                "rate=%s slewing=%s canpulse=%s canflip=%s" % (
                    a.RightAscension, a.Declination, raw, _pier_norm(raw),
                    (a.SiderealTime - a.RightAscension + 12.0) % 24.0 - 12.0,
                    a.SiderealTime, a.Tracking, a.TrackingRate, a.Slewing,
                    a.CanPulseGuide, a.CanSetPierSide))
    except Exception as e:
        return "ERR " + str(e)


def _wait_slew(a, timeout=SLEW_TIMEOUT_S):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if not a.Slewing:
                time.sleep(1.5)          # let the mount settle
                if not a.Slewing:
                    return None
        except Exception as e:
            return "slew poll failed: %s" % e
        time.sleep(0.4)
    try:
        a.AbortSlew()
    except Exception:
        pass
    return "slew timed out after %ds (aborted)" % timeout


def slew_to(arg, force_flip=False):
    """SLEW/FLIP <ra_hours> <dec_deg>.

    With force_flip, refuse unless the mount agrees the GOTO lands on the OTHER
    side of the pier, and verify afterwards that it actually did. Blindly
    assuming a flip happened is how an OTA ends up against a tripod leg.
    """
    try:
        ra_s, dec_s = arg.split()
        ra, dec = float(ra_s), float(dec_s)
    except Exception:
        return "ERR usage: SLEW <ra_hours> <dec_deg>"
    if not (0.0 <= ra < 24.0 and -90.0 <= dec <= 90.0):
        return "ERR coordinates out of range"
    try:
        a = _mount()
        if a.AtPark:
            return "ERR mount is parked"
        before = _pier(a)
        how = ""
        if force_flip:
            # An unknown starting side makes the whole flip unverifiable: the
            # "did the side change?" check below would pass vacuously. Refuse.
            # (The mount reports Unknown at the home position, Dec +90, where
            # pier side is geometrically meaningless.)
            if _pier_norm(before) == "?":
                return ("ERR refusing: mount reports pier side %s (parked/at "
                        "home?) — a flip cannot be verified from here" % before)
            dest, how = _dest_pier(a, ra)
            if dest == _pier_norm(before):
                return ("ERR refusing: GOTO would stay on pier side %s "
                        "[%s] — not past the meridian yet?" % (before, how))
        was_tracking = a.Tracking
        a.SlewToCoordinatesAsync(ra, dec)
        err = _wait_slew(a)
        if err:
            return "ERR " + err
        after = _pier(a)
        if force_flip and _pier_norm(after) == _pier_norm(before):
            return ("ERR slew finished but pier side is still %s — flip FAILED"
                    % after)
        if was_tracking and not a.Tracking:
            try:
                a.Tracking = True
            except Exception:
                pass
        return "OK slewed ra=%.6f dec=%.6f pier %s->%s%s" % (
            a.RightAscension, a.Declination, before, after,
            (" [dest via %s]" % how) if how else "")
    except Exception as e:
        return "ERR " + str(e)


def nudge(arg):
    """NUDGE <dRA_arcsec> <dDec_arcsec> — small ON-SKY relative correction.

    Commanded relative to the mount's own reported position, so a static
    pointing-model error cancels out. Guarded so a bad centroid can't fling
    the OTA across the sky or trip a pier flip mid-centring.
    """
    try:
        dra_s, ddec_s = arg.split()
        dra, ddec = float(dra_s), float(ddec_s)
    except Exception:
        return "ERR usage: NUDGE <dRA_arcsec> <dDec_arcsec>"
    if abs(dra) > MAX_NUDGE_ARCSEC or abs(ddec) > MAX_NUDGE_ARCSEC:
        return "ERR correction too large (%.0f,%.0f arcsec) — refusing" % (dra, ddec)
    try:
        a = _mount()
        dec0 = a.Declination
        cosd = math.cos(math.radians(max(-89.5, min(89.5, dec0))))
        if abs(cosd) < 1e-3:
            return "ERR too close to the pole for an RA offset"
        ra_new = (a.RightAscension + dra / 3600.0 / 15.0 / cosd) % 24.0
        dec_new = max(-90.0, min(90.0, dec0 + ddec / 3600.0))
        before = _pier_norm(_pier(a))
        dest, _how = _dest_pier(a, ra_new)
        if before != "?" and dest != before:
            return "ERR refusing nudge: it would change pier side"
        a.SlewToCoordinatesAsync(ra_new, dec_new)
        err = _wait_slew(a, timeout=60)
        if err:
            return "ERR " + err
        return "OK nudged dRA=%.1f dDec=%.1f arcsec" % (dra, ddec)
    except Exception as e:
        return "ERR " + str(e)


def set_tracking(arg):
    """TRACK ON|OFF [LUNAR|SIDEREAL|SOLAR]

    Drivers disagree on how the DriveRates enum is spelled -- the ASCOM
    constants are driveSidereal/driveLunar/..., but this mount's enum prints
    as plain "Sidereal". So match case-insensitively on a substring of the
    actual member names rather than assuming either spelling.

    Enabling tracking is the critical part and is reported as an error if it
    fails; the RATE is best-effort, because after a flip it is far better to
    be tracking at the wrong rate (and centring) than not tracking at all.
    """
    parts = arg.split()
    if not parts:
        return "ERR usage: TRACK ON|OFF [LUNAR|SIDEREAL|SOLAR]"
    want = parts[0].upper() in ("ON", "1", "TRUE")
    warn = ""
    try:
        a = _mount()
        if len(parts) > 1:
            wanted = parts[1].strip().lower()
            try:
                import System
                enum_t = a.TrackingRate.GetType()
                names = list(System.Enum.GetNames(enum_t))
                pick = None
                for n in names:
                    if wanted in n.lower():
                        pick = n
                        break
                if pick is None:
                    warn = " (rate %r not available; have: %s)" % (
                        wanted, ",".join(names))
                else:
                    a.TrackingRate = System.Enum.Parse(enum_t, pick)
            except Exception as e:
                warn = " (rate not set: %s)" % e
        a.Tracking = want
        time.sleep(0.5)
        if bool(a.Tracking) != want:
            return ("ERR mount refused tracking=%s (past a meridian limit?)"
                    % want)
        return "OK tracking=%s rate=%s%s" % (a.Tracking, a.TrackingRate, warn)
    except Exception as e:
        return "ERR " + str(e)


def sync_to(arg):
    """SYNC <ra_hours> <dec_deg> — tell the mount where it is actually pointing.

    This rewrites the mount's pointing model, so it is only ever correct when
    the scope really is on the given coordinates. The CALLER is responsible for
    proving that (the Pi checks the Moon is centred in the camera first); this
    end just refuses the obviously-wrong cases and reports the correction it
    applied so the size of the fix is visible.
    """
    try:
        ra_s, dec_s = arg.split()
        ra, dec = float(ra_s), float(dec_s)
    except Exception:
        return "ERR usage: SYNC <ra_hours> <dec_deg>"
    if not (0.0 <= ra < 24.0 and -90.0 <= dec <= 90.0):
        return "ERR coordinates out of range"
    try:
        a = _mount()
        if not a.CanSync:
            return "ERR this mount reports CanSync = false"
        if a.AtPark:
            return "ERR mount is parked"
        if a.Slewing:
            return "ERR mount is slewing — wait for it to settle"
        before_ra, before_dec = a.RightAscension, a.Declination
        a.SyncToCoordinates(ra, dec)
        # The driver refreshes its reported position on its own poll cycle, so
        # reading straight back reports "nothing moved" even on a good sync.
        # Wait until it actually reflects the new model, or give up saying so.
        after_ra, after_dec = before_ra, before_dec
        for _ in range(20):
            time.sleep(0.25)
            after_ra, after_dec = a.RightAscension, a.Declination
            if abs(after_ra - ra) < 0.01 and abs(after_dec - dec) < 0.15:
                break
        # how far the model just moved, on the sky
        cosd = math.cos(math.radians(max(-89.9, min(89.9, dec))))
        dra = (after_ra - before_ra) * 15.0 * cosd
        ddec = after_dec - before_dec
        moved = math.sqrt(dra * dra + ddec * ddec)
        settled = (abs(after_ra - ra) < 0.01 and abs(after_dec - dec) < 0.15)
        return ("OK synced to ra=%.6f dec=%.6f | model shifted %.3f deg "
                "(dRA %+.3f dDec %+.3f) | was ra=%.6f dec=%.6f%s" % (
                    ra, dec, moved, dra, ddec, before_ra, before_dec,
                    "" if settled else " | WARN driver did not confirm the new "
                                       "position within 5s"))
    except Exception as e:
        return "ERR " + str(e)


# --- flat-panel cover (ASCOM CoverCalibrator) ------------------------------
# The Deep Sky Dad FP2 is a serial device on a COM port, and a COM port has
# exactly one owner. Its own Control Panel app holds COM3 while it is
# connected, so this driver cannot open the port at the same time -- the app
# must be disconnected (its DISCONNECT button) or closed. We therefore connect
# only for the moment an action takes, and let go again, so the app stays
# usable the rest of the time.
COVER_PROGID = ""          # set via  COVER PROGID <id>  once, or leave to autodetect
_COVER_STATES = {0: "NotPresent", 1: "Closed", 2: "Moving", 3: "Open",
                 4: "Unknown", 5: "Error"}


def _cover_drivers():
    """Installed ASCOM CoverCalibrator drivers as (progid, name) pairs.

    Read straight from the ASCOM Profile's registry keys rather than through
    ASCOM.Utilities: inside SharpCap's IronPython, clr.AddReference cannot
    resolve the ASCOM assemblies by name (they are not on its probing path),
    and it fails with a bare HRESULT. The registry is where the Profile keeps
    this anyway, and Microsoft.Win32 is always available.
    """
    errs = []

    # 1. The ASCOM Profile is itself a COM server, so it can be driven with
    #    the same late binding used for the driver -- no assembly reference,
    #    no import, nothing that IronPython has to resolve by name.
    try:
        from System import Type, Activator
        t = Type.GetTypeFromProgID("ASCOM.Utilities.Profile")
        if t is not None:
            p = Activator.CreateInstance(t)
            try:
                p.DeviceType = "CoverCalibrator"
                out = []
                for d in p.RegisteredDevices("CoverCalibrator"):
                    out.append((str(d.Key), str(getattr(d, "Value", "") or "")))
                if out:
                    return out
            finally:
                try:
                    from System.Runtime.InteropServices import Marshal
                    Marshal.ReleaseComObject(p)
                except Exception:
                    pass
    except Exception as e:
        errs.append("Profile COM: %s" % e)

    # 2. Failing that, the registry -- but Microsoft.Win32.Registry lives in
    #    its own assembly on modern .NET and is not imported by default.
    try:
        try:
            from Microsoft.Win32 import Registry
        except ImportError:
            import clr
            clr.AddReference("Microsoft.Win32.Registry")
            from Microsoft.Win32 import Registry
        out = []
        for path in (r"SOFTWARE\ASCOM\CoverCalibrator Drivers",
                     r"SOFTWARE\WOW6432Node\ASCOM\CoverCalibrator Drivers"):
            key = Registry.LocalMachine.OpenSubKey(path)
            if key is None:
                continue
            try:
                for name in key.GetSubKeyNames():
                    sub = key.OpenSubKey(name)
                    desc = ""
                    try:
                        if sub is not None:
                            desc = str(sub.GetValue("") or "")
                    finally:
                        if sub is not None:
                            sub.Close()
                    if not any(name == k for k, _ in out):
                        out.append((str(name), desc))
            finally:
                key.Close()
        if out:
            return out
    except Exception as e:
        errs.append("registry: %s" % e)

    if errs:
        raise RuntimeError(
            "could not enumerate CoverCalibrator drivers (%s). Set it by hand "
            "with  COVER PROGID <id>  -- the ProgID is shown by ASCOM "
            "Diagnostics, or in the FP2's ASCOM setup dialog"
            % "; ".join(errs))
    return []


def _cover_open_driver():
    """Connect to the cover through plain COM late binding.

    An ASCOM driver is a COM server, so it can be created from its ProgID with
    no .NET assembly references at all -- which sidesteps the AddReference
    failure completely and works whatever the ASCOM Platform version.
    """
    from System import Type, Activator
    progid = COVER_PROGID
    if not progid:
        found = _cover_drivers()
        if not found:
            raise RuntimeError("no ASCOM CoverCalibrator driver is installed")
        if len(found) > 1:
            raise RuntimeError(
                "%d CoverCalibrator drivers installed (%s) — pick one with "
                "COVER PROGID <id>" % (len(found), ", ".join(f[0] for f in found)))
        progid = found[0][0]
    t = Type.GetTypeFromProgID(progid)
    if t is None:
        raise RuntimeError("ProgID %r is not registered on this machine" % progid)
    c = Activator.CreateInstance(t)
    c.Connected = True
    return c


def cover(arg):
    """COVER STATE|CLOSE|OPEN|LIST|SETTINGS [filter]|PROBE [progid...]|PROGID <id>

    CLOSE and OPEN do not return until the cover has actually reached the
    state, or they say why not. "I sent the command" is not good enough when
    the thing being protected from is the Sun.
    """
    global COVER_PROGID
    parts = (arg or "").split()
    what = (parts[0].upper() if parts else "STATE")
    try:
        if what == "LIST":
            found = _cover_drivers()
            if not found:
                return "ERR no ASCOM CoverCalibrator driver installed"
            return "OK " + " | ".join("%s (%s)" % (k, v) for k, v in found)
        if what == "SETTINGS":
            # Deep Sky Dad registers six numbered CoverCalibrator instances and
            # only one of them is configured for the port the panel is on. Each
            # instance's settings live in the ASCOM Profile, so read them and
            # let the COM port say which is which. Read-only: this never opens
            # the port, so it cannot fight the FP2's Control Panel app.
            from System import Type, Activator
            filt = (parts[1].lower() if len(parts) > 1 else "deepskydad")
            t = Type.GetTypeFromProgID("ASCOM.Utilities.Profile")
            if t is None:
                return "ERR the ASCOM Profile COM object is not available"
            p = Activator.CreateInstance(t)
            try:
                p.DeviceType = "CoverCalibrator"
                out = []
                for pid, _desc in _cover_drivers():
                    if filt not in pid.lower():
                        continue
                    bits = []
                    try:
                        for kv in p.Values(pid):
                            k = str(kv.Key)
                            v = str(getattr(kv, "Value", "") or "")
                            if k:
                                bits.append("%s=%s" % (k, v))
                    except Exception as e:
                        bits.append("<%s>" % e)
                    out.append("%s: %s" % (pid, ", ".join(bits) or "<no settings>"))
                if not out:
                    return "ERR no CoverCalibrator ProgID matches %r" % filt
                return "OK " + " | ".join(out)
            finally:
                try:
                    from System.Runtime.InteropServices import Marshal
                    Marshal.ReleaseComObject(p)
                except Exception:
                    pass
        if what == "PROBE":
            # If enumeration is unavailable, ask Windows directly whether each
            # plausible ProgID is registered. GetTypeFromProgID only does a
            # registry lookup -- it does not create or connect anything -- so
            # this is free and cannot disturb the device.
            from System import Type
            cands = list(parts[1:]) or [
                "ASCOM.DeepSkyDad.FP2.CoverCalibrator",
                "ASCOM.DeepSkyDadFP2.CoverCalibrator",
                "ASCOM.DeepSkyDad.FP.CoverCalibrator",
                "ASCOM.DSD.FP2.CoverCalibrator",
                "ASCOM.DeepSkyDadFlatPanel.CoverCalibrator",
                "ASCOM.Simulator.CoverCalibrator",
            ]
            hits = [c for c in cands if Type.GetTypeFromProgID(c) is not None]
            if not hits:
                return ("ERR none of these are registered: %s — find the real "
                        "ProgID in ASCOM Diagnostics (Choose Device > "
                        "CoverCalibrator) and set it with COVER PROGID <id>"
                        % ", ".join(cands))
            return "OK registered: " + ", ".join(hits)
        if what == "PROGID":
            if len(parts) < 2:
                return "OK progid=%s" % (COVER_PROGID or "<autodetect>")
            COVER_PROGID = parts[1]
            return "OK progid set to %s" % COVER_PROGID

        c = _cover_open_driver()
        try:
            state = int(c.CoverState)
            if what == "STATE":
                return "OK state=%s(%d)" % (_COVER_STATES.get(state, "?"), state)
            if what not in ("CLOSE", "OPEN"):
                return ("ERR usage: COVER STATE|CLOSE|OPEN|LIST|SETTINGS|"
                        "PROBE|PROGID <id>")
            want = 1 if what == "CLOSE" else 3
            if state == want:
                return "OK already %s" % _COVER_STATES[want]
            if state == 0:
                return "ERR driver reports NotPresent — is the FP2 powered and " \
                       "its Control Panel app disconnected from the COM port?"
            if what == "CLOSE":
                c.CloseCover()
            else:
                c.OpenCover()
            t0 = time.time()
            while time.time() - t0 < 90.0:
                time.sleep(1.0)
                state = int(c.CoverState)
                if state == want:
                    return "OK cover %s after %.0fs" % (_COVER_STATES[want],
                                                        time.time() - t0)
                if state == 5:
                    return "ERR driver reports Error while moving the cover"
                if state != 2:                # not Moving and not there yet
                    return "ERR cover stopped at %s(%d)" % (
                        _COVER_STATES.get(state, "?"), state)
            return "ERR cover did not reach %s within 90s (now %s)" % (
                _COVER_STATES[want], _COVER_STATES.get(state, "?"))
        finally:
            # Release COM3 so the FP2's own Control Panel can use it again.
            # A COM object has no Dispose(); drop the RCW explicitly instead.
            try:
                c.Connected = False
            except Exception:
                pass
            try:
                from System.Runtime.InteropServices import Marshal
                Marshal.ReleaseComObject(c)
            except Exception:
                pass
    except Exception as e:
        return "ERR " + str(e)


def park_mount(arg):
    """PARK — stop tracking and send the mount somewhere safe for daylight.

    Tries, in order: the driver's own Park, then FindHome, then a plain slew to
    the pole with tracking off. Whichever runs, tracking is stopped LAST-resort
    regardless, because a mount left tracking through the day walks the OTA
    towards wherever the Sun happens to be.
    """
    try:
        a = _mount()
        if a.AtPark:
            return "OK already parked"
        how = []
        try:
            a.Tracking = False
            how.append("tracking off")
        except Exception as e:
            how.append("could NOT stop tracking (%s)" % e)
        if getattr(a, "CanPark", False):
            a.Park()
            t0 = time.time()
            while time.time() - t0 < SLEW_TIMEOUT_S:
                if a.AtPark:
                    return "OK parked [driver Park] (%s)" % ", ".join(how)
                time.sleep(1.0)
            return "ERR Park() did not complete within %ds (%s)" % (
                SLEW_TIMEOUT_S, ", ".join(how))
        if getattr(a, "CanFindHome", False):
            a.FindHome()
            err = _wait_slew(a)
            if err:
                return "ERR FindHome: %s" % err
            return "OK homed [FindHome] (%s)" % ", ".join(how)
        # Last resort: the pole. Nothing to hit, and the aperture is as far
        # from the ecliptic as this mount can put it.
        a.SlewToCoordinatesAsync(a.SiderealTime % 24.0, 89.9)
        err = _wait_slew(a)
        if err:
            return "ERR slew to pole: %s" % err
        try:
            a.Tracking = False
        except Exception:
            pass
        return "OK slewed to the pole (no Park or FindHome in this driver) (%s)" % (
            ", ".join(how))
    except Exception as e:
        return "ERR " + str(e)


def mount_actions(arg):
    """ACTIONS — READ ONLY. What custom ASCOM actions does this driver offer?

    The ASI driver advertises ShutdownIfIdle and BeginShutdown; knowing exactly
    what they are named is the difference between a clean daylight shutdown and
    a guess.
    """
    try:
        a = _mount()
        acts = []
        for x in a.SupportedActions:
            acts.append(str(x))
        return "OK CanPark=%s CanFindHome=%s AtPark=%s CanSetPark=%s actions=%s" % (
            getattr(a, "CanPark", "?"), getattr(a, "CanFindHome", "?"),
            getattr(a, "AtPark", "?"), getattr(a, "CanSetPark", "?"),
            ",".join(acts))
    except Exception as e:
        return "ERR " + str(e)


def mount_caps():
    """CAPS — READ ONLY. Driver properties that affect flip correctness.

    Two matter most:
      * EquatorialSystem — if the driver wants J2000 but we send apparent
        (JNow) coordinates, the flip slews to the wrong place by up to ~0.3 deg.
      * AlignmentMode    — the pier-side geometry only holds for a German
        equatorial (algGermanPolar).
    SupportedActions is included because vendor-specific settings such as the
    meridian limit, which is stored in the mount rather than on the PC, are
    only ever reachable through a custom Action if at all.
    """
    try:
        a = _mount()
        out = []
        for name in ("EquatorialSystem", "AlignmentMode", "SlewSettleTime",
                     "DoesRefraction", "SiteElevation", "SiteLatitude",
                     "GuideRateRightAscension", "GuideRateDeclination",
                     "CanSetPierSide", "CanSetTracking", "CanSlewAsync",
                     "CanSync", "CanPulseGuide", "InterfaceVersion",
                     "DriverVersion", "Name"):
            try:
                out.append("%s=%s" % (name, getattr(a, name)))
            except Exception as e:
                out.append("%s=<%s>" % (name, str(e)[:40]))
        try:
            acts = list(a.SupportedActions)
            out.append("SupportedActions=[%s]" % ",".join(str(x) for x in acts)
                       if acts else "SupportedActions=[]")
        except Exception as e:
            out.append("SupportedActions=<%s>" % str(e)[:40])
        try:
            rates = [str(r) for r in a.TrackingRates]
            out.append("TrackingRates=[%s]" % ",".join(rates))
        except Exception as e:
            out.append("TrackingRates=<%s>" % str(e)[:40])
        return "OK " + " ".join(out)
    except Exception as e:
        return "ERR " + str(e)


def pier_check(arg):
    """PIERCHK <ra_hours> <dec_deg> — READ ONLY.

    Asks the driver which side of the pier a GOTO to those coordinates would
    end on. The whole flip design hinges on this being implemented: many
    drivers that report CanSetPierSide=False also throw on
    DestinationSideOfPier, and this reports that without moving anything.
    """
    try:
        ra_s, dec_s = arg.split()
        ra, dec = float(ra_s), float(dec_s)
    except Exception:
        return "ERR usage: PIERCHK <ra_hours> <dec_deg>"
    try:
        a = _mount()
        now = _pier(a)
        dest, how = _dest_pier(a, ra)
        cur = _pier_norm(now)
        return "OK current=%s(%s) destination=%s via=%s would_flip=%s" % (
            now, cur, dest, how,
            "yes" if (cur != "?" and dest != cur) else "no")
    except Exception as e:
        return "ERR " + str(e)


def _grab_frame(cam, path, timeout=25.0):
    """Capture ONE fresh frame to `path`, or explain why not.

    CaptureSingleFrameTo can return without writing anything (and can block
    outright when the camera is not live). Analysing whatever happens to be on
    disk then silently measures a stale image -- which is exactly how a flip
    "succeeded" against a frame captured two hours earlier. So: remove the old
    file first, bound the call, and require a new one to exist afterwards.
    """
    import os
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as e:
        return "cannot clear the old frame (%s) -- is it open elsewhere?" % e

    box = {}

    def _cap():
        try:
            cam.CaptureSingleFrameTo(path)
            box["done"] = True
        except Exception as e:
            box["err"] = str(e)

    t = threading.Thread(target=_cap)
    t.setDaemon(True)
    t.start()
    t.join(timeout)
    if t.isAlive():
        # Leave the thread; the listener must stay responsive for STOP/MOUNT.
        return ("frame capture did not return within %.0fs -- is the camera "
                "running (live view) in SharpCap?" % timeout)
    if "err" in box:
        return "capture failed: %s" % box["err"]
    if not os.path.exists(path):
        return ("capture reported success but wrote no file -- camera not "
                "live, or the path is not writable")
    return None


def moon_position(arg):
    """MOONPOS — snap a FRESH frame and locate the lunar disc in it.

    Returns the bounding box of the lit region. Its centre is the disc centre
    for a gibbous/full Moon, and its LONGEST side is the full diameter at any
    phase (the terminator only cuts the short axis) -- which lets the caller
    derive arcsec/pixel from the Moon's known angular size, with no focal
    length or pixel size needed.
    """
    try:
        cam = SharpCap.SelectedCamera  # noqa: F821
        if cam is None:
            return "ERR no camera"
        path = arg or SNAP_PATH
        why = _grab_frame(cam, path)
        if why:
            return "ERR " + why

        import clr
        clr.AddReference("System.Drawing")
        from System.Drawing import Bitmap, Rectangle
        from System.Drawing.Imaging import ImageLockMode, PixelFormat
        from System.Runtime.InteropServices import Marshal
        from System import Array, Byte

        bmp = Bitmap(path)
        try:
            w, h = bmp.Width, bmp.Height
            # GetPixel per sample is ~60x slower here; lock the bits once and
            # walk raw bytes instead. A full-frame scan was taking a minute.
            rect = Rectangle(0, 0, w, h)
            bd = bmp.LockBits(rect, ImageLockMode.ReadOnly, PixelFormat.Format24bppRgb)
            try:
                stride = bd.Stride
                buf = Array.CreateInstance(Byte, stride * h)
                Marshal.Copy(bd.Scan0, buf, 0, stride * h)
            finally:
                bmp.UnlockBits(bd)

            step = max(1, int(max(w, h) / 240))
            vals = []
            y = 0
            while y < h:
                row = y * stride
                x = 0
                while x < w:
                    vals.append(buf[row + x * 3 + 2])   # BGR -> red channel
                    x += step
                y += step
            if not vals:
                return "ERR no samples"
            vals_sorted = sorted(vals)
            peak = vals_sorted[-1]
            # Sky level from a low percentile: the Moon covers well under half
            # the frame, so the bottom decile is sky even when it is bright.
            sky = vals_sorted[len(vals_sorted) // 10]
            if peak - sky < 30:
                return ("ERR no disc: frame contrast too low "
                        "(sky=%d peak=%d) -- Moon not in field?" % (sky, peak))

            # Threshold RELATIVE TO SKY, low on the disc's dynamic range.
            # A fixed 45%-of-peak cut discards the dim limb near the terminator,
            # which truncates the bounding box on that side and drags the
            # measured centre off by hundreds of arcsec. Measured on a 97%-lit
            # Moon: 45% gave a 1008 px tall disc centred at y=1368, while 10%
            # converged to the true 1376 px tall disc centred at y=1184.
            thr = sky + 0.12 * (peak - sky)
            n = 0
            sx = sy = 0
            x0, y0, x1, y1 = w, h, -1, -1
            y = 0
            while y < h:
                row = y * stride
                x = 0
                while x < w:
                    if buf[row + x * 3 + 2] >= thr:
                        n += 1
                        sx += x
                        sy += y
                        if x < x0: x0 = x
                        if y < y0: y0 = y
                        if x > x1: x1 = x
                        if y > y1: y1 = y
                    x += step
                y += step
            if n < 12:
                return "ERR only %d lit samples -- no disc found" % n
            return ("OK cx=%.1f cy=%.1f bx0=%d by0=%d bx1=%d by1=%d "
                    "w=%d h=%d n=%d peak=%d sky=%d thr=%d step=%d" % (
                        float(sx) / n, float(sy) / n, x0, y0, x1, y1,
                        w, h, n, peak, sky, int(thr), step))
        finally:
            bmp.Dispose()
    except Exception as e:
        return "ERR " + str(e)


def _capturing(cam):
    """Robust 'is a capture running?' probe — property name varies by version."""
    for name in ("CaptureActive", "Capturing", "IsCapturing"):
        if hasattr(cam, name):
            try:
                return bool(getattr(cam, name))
            except Exception:
                pass
    return None  # unknown — treat as 'maybe'


def _force_unlimited(cam):
    """Clear any leftover capture time/frame limit from the UI so the capture
    runs until our STOP (or the failsafe). Returns a warning string or None."""
    try:
        cc = cam.CaptureConfig
    except Exception as e:
        return "no CaptureConfig: %s" % e
    warns = []
    for attr, target in (("CaptureLimitType", "Unlimited"),):
        if hasattr(cc, attr):
            try:
                import System  # noqa: F401 (.NET, available in IronPython)
                cur = getattr(cc, attr)
                enum_t = cur.GetType()
                names = list(System.Enum.GetNames(enum_t))
                pick = target if target in names else next(
                    (n for n in names if "unlimit" in n.lower() or n.lower() == "none"), None)
                if pick:
                    setattr(cc, attr, System.Enum.Parse(enum_t, pick))
                else:
                    warns.append("%s options: %s" % (attr, ",".join(names)))
            except Exception as e:
                warns.append("%s: %s" % (attr, e))
    return "; ".join(warns) or None


def start_capture():
    cam = SharpCap.SelectedCamera  # noqa: F821 (provided by SharpCap)
    if cam is None:
        return "ERR no camera selected"
    try:
        lim_warn = _force_unlimited(cam)
        # PrepareToCapture() creates the output writer; RunCapture() without it
        # fails with "No writer object when trying to initialize it".
        prep_warn = None
        if hasattr(cam, "PrepareToCapture"):
            try:
                cam.PrepareToCapture()
            except Exception as e:
                prep_warn = str(e)
        cam.RunCapture()
        threading.Timer(MAX_CAPTURE_S, stop_capture).start()
        warns = "; ".join(w for w in (lim_warn, prep_warn) if w)
        return "OK recording" + (" (warn: %s)" % warns if warns else "")
    except Exception as e:
        return "ERR " + str(e)


def stop_capture():
    cam = SharpCap.SelectedCamera  # noqa: F821
    if cam is None:
        return "ERR no camera selected"
    if _capturing(cam) is False:
        return "OK was not recording"
    try:
        cam.StopCapture()
        return "OK stopped"
    except Exception as e:
        return "ERR " + str(e)


def cam_info():
    """INFO command: report the camera's actual capture-related API members,
    so version differences can be diagnosed remotely."""
    cam = SharpCap.SelectedCamera  # noqa: F821
    if cam is None:
        return "ERR no camera selected"
    keys = ("captur", "writer", "prepare", "run", "stop", "record", "output")
    members = sorted(m for m in dir(cam) if any(k in m.lower() for k in keys))
    cfg = ""
    try:
        cc = cam.CaptureConfig
        fields = sorted(m for m in dir(cc) if "limit" in m.lower() or "capture" in m.lower())
        vals = []
        for f in fields[:8]:
            try:
                vals.append("%s=%s" % (f, getattr(cc, f)))
            except Exception:
                pass
        cfg = " | CaptureConfig: " + ",".join(vals)
    except Exception as e:
        cfg = " | CaptureConfig err: %s" % e
    return "OK " + ",".join(members) + cfg


# Which member holds the CAMERA's settings differs by SharpCap version, and
# the obvious guess is wrong: on 4.2 `SelectedCamera.Controls` is the WPF user
# -interface control collection inherited from the view, whose items are
# WPFPropertyControl and have no Value at all. So try the candidates in order
# and accept the first one whose items actually look like camera settings.
_CTRL_COLLECTIONS = ("Controls", "CameraControls", "Properties",
                     "ControlValues", "Settings")
_VALUE_MEMBERS = ("Value", "NumericValue", "ValueNumeric", "CurrentValue")


def _ctrl_value(c):
    """(member_name, value) for whichever member holds this control's value."""
    for m in _VALUE_MEMBERS:
        try:
            v = getattr(c, m)
        except Exception:
            continue
        if v is not None and not callable(v):
            return m, v
    return None, None


def _camera_controls(cam):
    """The camera's settings collection, or None if nothing looks right."""
    for attr in _CTRL_COLLECTIONS:
        try:
            coll = getattr(cam, attr)
            items = [c for c in coll]
        except Exception:
            continue
        for c in items:
            try:
                if getattr(c, "Name", None) and _ctrl_value(c)[0]:
                    return items
            except Exception:
                pass
    return None


def _describe_camera(cam):
    """Fallback for CTRLS: what DOES this camera object offer? Reported so the
    right accessor can be found from a real machine instead of guessed at."""
    out = []
    for attr in _CTRL_COLLECTIONS:
        try:
            coll = getattr(cam, attr)
        except Exception as e:
            out.append("%s: <%s>" % (attr, e))
            continue
        try:
            items = [c for c in coll]
            kinds = sorted(set(str(type(c)).split(".")[-1].strip("'>") for c in items))
            sample = ""
            if items:
                c = items[0]
                members = sorted(m for m in dir(c) if not m.startswith("_"))[:25]
                sample = " first-item members: " + ",".join(members)
            out.append("%s: %d items %s%s" % (attr, len(items), kinds, sample))
        except Exception as e:
            out.append("%s: not iterable (%s)" % (attr, e))
    keys = ("control", "propert", "gain", "expos", "setting")
    members = sorted(m for m in dir(cam)
                     if any(k in m.lower() for k in keys))
    out.append("camera members matching %s: %s" % (list(keys), ",".join(members)))
    return " | ".join(out)


def _find_control(cam, want):
    """Match a control by name, ignoring case, spaces and underscores."""
    key = want.lower().replace(" ", "").replace("_", "")
    items = _camera_controls(cam)
    if not items:
        return None
    for c in items:
        try:
            nm = getattr(c, "Name", None)
        except Exception:
            continue
        if nm and str(nm).lower().replace(" ", "").replace("_", "") == key:
            return c
    return None


def cam_controls(arg):
    """CTRLS [name] — READ ONLY. Report the camera's controls.

    Names and ranges differ between cameras and between SharpCap versions, so
    nothing on the Pi hard-codes them: it asks, matches on what comes back, and
    works with whatever this camera actually exposes.
    """
    try:
        cam = SharpCap.SelectedCamera  # noqa: F821
        if cam is None:
            return "ERR no camera"
        items = _camera_controls(cam)
        if items is None:
            return "ERR no usable control collection — " + _describe_camera(cam)
        out = []
        for c in items:
            try:
                nm = getattr(c, "Name", "?")
            except Exception:
                continue
            if arg and arg.strip().lower() not in str(nm).lower():
                continue
            bits = ["name=%s" % nm]
            vm, val = _ctrl_value(c)
            if vm:
                bits.append("Value=%s" % val)
                if vm != "Value":
                    bits.append("ValueMember=%s" % vm)
            for f in ("MinValue", "Minimum", "MaximumValue", "MaxValue", "Maximum",
                      "StepSize", "Increment", "Automatic", "IsAuto", "Unit",
                      "Unity", "ValueUnit"):
                try:
                    v = getattr(c, f)
                except Exception:
                    continue
                if v is not None and not callable(v):
                    bits.append("%s=%s" % (f, v))
            out.append(";".join(bits))
        if not out:
            return "ERR no matching control"
        return "OK " + " | ".join(out)
    except Exception as e:
        return "ERR " + str(e)


def cam_control_set(arg):
    """CTRL <name> <value> — set one camera control.

    Refused while a capture is running: a gain change mid-recording puts a
    brightness step through the middle of a transit, and the transit is the
    entire point of the recording.
    """
    try:
        parts = arg.split()
        if len(parts) < 2:
            return "ERR usage: CTRL <name> <value>"
        want, val = parts[0], float(parts[1])
        cam = SharpCap.SelectedCamera  # noqa: F821
        if cam is None:
            return "ERR no camera"
        if _capturing(cam):
            return "ERR refusing: a capture is running"
        c = _find_control(cam, want)
        if c is None:
            items = _camera_controls(cam)
            if items is None:
                return "ERR no usable control collection on this camera"
            names = []
            for k in items:
                try:
                    names.append(str(getattr(k, "Name", "?")))
                except Exception:
                    pass
            return "ERR no control named %r — have: %s" % (want, ", ".join(names))
        vm, before = _ctrl_value(c)
        if not vm:
            return "ERR control %r exposes no writable value" % want
        for lo_name in ("MinValue", "Minimum"):
            lo = getattr(c, lo_name, None)
            if lo is not None and not callable(lo):
                if val < lo:
                    return "ERR %s below minimum (%s)" % (want, lo)
                break
        for hi_name in ("MaximumValue", "MaxValue", "Maximum"):
            hi = getattr(c, hi_name, None)
            if hi is not None and not callable(hi):
                if val > hi:
                    return "ERR %s above maximum (%s)" % (want, hi)
                break
        setattr(c, vm, val)
        return "OK %s %s -> %s" % (getattr(c, "Name", want), before,
                                   _ctrl_value(c)[1])
    except Exception as e:
        return "ERR " + str(e)


def dir_of(path):
    """DIR <dotted.path> — introspect any SharpCap scripting object remotely,
    e.g. 'DIR Focusers.SelectedFocuser'. Rooted at the SharpCap object."""
    try:
        obj = SharpCap  # noqa: F821
        for part in [p for p in path.split(".") if p and p != "SharpCap"]:
            obj = getattr(obj, part)
        members = sorted(m for m in dir(obj) if not m.startswith("_"))
        return "OK %s: %s" % (type(obj).__name__, ",".join(members))
    except Exception as e:
        return "ERR " + str(e)


def focuser_info():
    try:
        f = SharpCap.Focusers.SelectedFocuser  # noqa: F821
        if f is None:
            return "ERR no focuser selected"
        out = []
        for name in ("Position", "Temperature", "MaxPosition", "MinPosition",
                     "IsMoving", "HasTemperature"):
            if hasattr(f, name):
                try:
                    out.append("%s=%s" % (name, getattr(f, name)))
                except Exception:
                    pass
        return "OK " + ",".join(out)
    except Exception as e:
        return "ERR " + str(e)


def handle(conn, addr):
    try:
        conn.settimeout(10)
        data = conn.recv(256)
        line = data.decode("utf-8", "replace").strip() if data else ""
        parts = line.split(None, 1)
        cmd = parts[0].upper() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""
        print("[lunar] %s -> %s %s" % (addr[0], cmd, arg))
        if cmd == "REC":
            reply = start_capture()
        elif cmd == "STOP":
            reply = stop_capture()
        elif cmd == "PING":
            reply = "PONG v%s" % LISTENER_VERSION
        elif cmd == "VERSION":
            reply = "OK version=%s commands=%s" % (LISTENER_VERSION, ",".join(sorted([
                "REC", "STOP", "PING", "VERSION", "INFO", "DIR", "TEMP", "FPOS",
                "FMOVE", "CAPST", "SNAP", "MOUNT", "SLEW", "FLIP", "NUDGE",
                "TRACK", "MOONPOS", "PIERCHK", "CAPS", "SYNC", "CTRLS", "CTRL",
                "PARK", "ACTIONS", "COVER"])))
        elif cmd == "INFO":
            reply = cam_info()
        elif cmd == "DIR":
            reply = dir_of(arg)
        elif cmd == "MOUNT":
            reply = mount_state()
        elif cmd == "SLEW":
            reply = slew_to(arg)
        elif cmd == "FLIP":
            reply = slew_to(arg, force_flip=True)
        elif cmd == "NUDGE":
            reply = nudge(arg)
        elif cmd == "TRACK":
            reply = set_tracking(arg)
        elif cmd == "MOONPOS":
            reply = moon_position(arg)
        elif cmd == "PIERCHK":
            reply = pier_check(arg)
        elif cmd == "CAPS":
            reply = mount_caps()
        elif cmd == "SYNC":
            reply = sync_to(arg)
        elif cmd == "TEMP":
            reply = focuser_info()
        elif cmd == "FPOS":
            f = SharpCap.Focusers.SelectedFocuser  # noqa: F821
            reply = "ERR no focuser" if f is None else "OK %d" % f.Position
        elif cmd == "FMOVE":
            f = SharpCap.Focusers.SelectedFocuser  # noqa: F821
            if f is None:
                reply = "ERR no focuser"
            else:
                try:
                    lo = int(getattr(f, "Mininum", 0) or 0)      # (sic) SharpCap API
                    hi = int(getattr(f, "Maximum", 0) or 200000)
                    tgt = max(lo, min(hi, int(arg)))
                    f.Move(tgt)
                    reply = "OK moving to %d" % tgt
                except Exception as e:
                    reply = "ERR " + str(e)
        elif cmd == "COVER":
            reply = cover(arg)
        elif cmd == "PARK":
            reply = park_mount(arg)
        elif cmd == "ACTIONS":
            reply = mount_actions(arg)
        elif cmd == "CTRLS":
            reply = cam_controls(arg)
        elif cmd == "CTRL":
            reply = cam_control_set(arg)
        elif cmd == "CAPST":
            cam = SharpCap.SelectedCamera  # noqa: F821
            f = SharpCap.Focusers.SelectedFocuser  # noqa: F821
            reply = "OK capturing=%s moving=%s pos=%s temp=%s" % (
                _capturing(cam) if cam else None,
                f.IsMoving if f is not None else None,
                f.Position if f is not None else None,
                getattr(f, "Temperature", None) if f is not None else None)
        elif cmd == "SNAP":
            cam = SharpCap.SelectedCamera  # noqa: F821
            if cam is None:
                reply = "ERR no camera"
            else:
                try:
                    path = arg or r"C:\LunarTransit\af_frame.png"
                    cam.CaptureSingleFrameTo(path)
                    reply = "OK " + path
                except Exception as e:
                    reply = "ERR " + str(e)
        else:
            reply = "ERR unknown command"
        conn.sendall((reply + "\n").encode("utf-8"))
        print("[lunar] reply: %s" % reply)
    except Exception:
        traceback.print_exc()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def serve():
    """Bind the command port, replacing any listener a previous run left behind.

    Re-running this script in the same SharpCap session used to leave TWO
    listeners on port 5580. On Windows -- unlike Unix -- SO_REUSEADDR lets a
    second socket bind a port that is already bound, and Windows then hands
    connections to whichever it likes. That is not theoretical: a stray second
    listener on this port once swallowed the Pi's commands and drove the real
    camera into a 49-second recording nobody asked for.

    So the previous run's socket is closed first, and SO_REUSEADDR is set ONLY
    when there was a previous run to replace. That distinction is the whole
    trick: taking the port back off ourselves is legitimate and needs the flag
    (the old socket lingers briefly after close), while taking it off a stranger
    is the bug, and without the flag that bind simply fails and says so.

    The handle is stashed on `sys` rather than in a module global because
    re-running the script re-executes this file from the top, which would reset
    any global here before it could be read.
    """
    prev = getattr(sys, "_lunar_listener_srv", None)
    if prev is not None:
        try:
            prev.close()          # makes the old accept() raise, ending its loop
            print("[lunar] closed the previous listener on port %d" % PORT)
        except Exception:
            pass

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if prev is not None:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    bound, why = False, None
    for _ in range(12):           # the old socket can linger a moment
        try:
            srv.bind(("0.0.0.0", PORT))
            bound = True
            break
        except Exception as e:
            why = e
            if prev is None:
                break             # a stranger holds it; waiting will not help
            time.sleep(0.5)
    if not bound:
        # Loud failure beats a silent second listener stealing half the traffic.
        print("[lunar] CANNOT BIND PORT %d: %s" % (PORT, why))
        print("[lunar] something else is holding it — restart SharpCap")
        return
    sys._lunar_listener_srv = srv
    srv.listen(2)
    print("[lunar] capture listener v%s on port %d" % (LISTENER_VERSION, PORT))
    while True:
        try:
            conn, addr = srv.accept()
        except Exception:
            # Socket closed by a newer run of this script: retire quietly.
            if getattr(sys, "_lunar_listener_srv", None) is not srv:
                print("[lunar] listener replaced by a newer run — exiting")
            else:
                print("[lunar] listener socket closed — exiting")
            return
        threading.Thread(target=handle, args=(conn, addr)).start()


threading.Thread(target=serve).start()
print("[lunar] listener thread started - waiting for REC/STOP from the Pi")
