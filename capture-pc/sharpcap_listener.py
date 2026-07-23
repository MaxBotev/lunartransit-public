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

import socket
import threading
import traceback

PORT = 5580
MAX_CAPTURE_S = 120  # safety: force-stop if STOP never arrives


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
