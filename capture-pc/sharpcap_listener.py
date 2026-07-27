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
import threading
import time
import traceback

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
            reply = "PONG"
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
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", PORT))
    srv.listen(2)
    print("[lunar] capture listener on port %d" % PORT)
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=handle, args=(conn, addr)).start()


threading.Thread(target=serve).start()
print("[lunar] listener thread started - waiting for REC/STOP from the Pi")
