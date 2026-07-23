#!/usr/bin/env python3
"""
LunarADSB supervisor — the visible console for the decoder chain.

Replaces the old bat loop (whose duplicate instances fought over a shared log
file). Single-instance guarded, one writer, ANSI color-coded rolling log:

  cyan  INFO   attach / lifecycle events
  green OK     periodic decode stats (aircraft, message rate, data age)
  yellow WARN  recoverable trouble (stick missing, stale data, retries)
  red   ERR    failures
  gray         raw dump1090/usbipd output

Runs as the LunarADSB scheduled task (console window visible at logon —
minimize it, or watch the traffic roll by).
"""

import ctypes
import json
import os
import socket
import subprocess
import sys
import threading
import time

VIDPID = "0bda:2838"          # any RTL2832U stick (FlightStick, RTL-SDR v3/v4)
AIRCRAFT_JSON = r"C:\LunarTransit\adsb_local\aircraft.json"
STATS_EVERY = 10.0
GUARD_PORT = 58581            # single-instance lock

# ---- console setup ---------------------------------------------------------
ctypes.windll.kernel32.SetConsoleTitleW("LunarADSB Supervisor")
# enable ANSI escape processing on the console
h = ctypes.windll.kernel32.GetStdHandle(-11)
mode = ctypes.c_uint32()
ctypes.windll.kernel32.GetConsoleMode(h, ctypes.byref(mode))
ctypes.windll.kernel32.SetConsoleMode(h, mode.value | 0x0004)

C = {"INFO": "\x1b[36m", "OK": "\x1b[32m", "WARN": "\x1b[33m",
     "ERR": "\x1b[31m", "RAW": "\x1b[90m", "R": "\x1b[0m"}


def log(level, msg):
    ts = time.strftime("%H:%M:%S")
    print(f"{C[level]}[{ts}] {level:<4} {msg}{C['R']}", flush=True)


def raw(line):
    line = line.rstrip()
    if not line:
        return
    low = line.lower()
    lvl = "ERR" if ("error" in low or "fail" in low) else \
          "WARN" if ("exit" in low or "no stick" in low or "retry" in low) else "RAW"
    print(f"{C[lvl]}[{time.strftime('%H:%M:%S')}]      {line}{C['R']}", flush=True)


# ---- single instance -------------------------------------------------------
guard = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    guard.bind(("127.0.0.1", GUARD_PORT))
    guard.listen(1)
except OSError:
    log("ERR", "another LunarADSB supervisor is already running — exiting")
    time.sleep(4)
    sys.exit(1)

log("INFO", "LunarADSB supervisor starting (Ctrl+C or stop_lunar.bat to stop)")


# ---- periodic decode stats -------------------------------------------------
_last = {"messages": None, "t": None}


def stats_loop():
    while True:
        time.sleep(STATS_EVERY)
        try:
            age = time.time() - os.path.getmtime(AIRCRAFT_JSON)
            d = json.load(open(AIRCRAFT_JSON))
            msgs = d.get("messages", 0)
            n = len(d.get("aircraft", []))
            npos = sum(1 for a in d.get("aircraft", []) if a.get("lat") is not None)
            rate = ""
            if _last["messages"] is not None and msgs >= _last["messages"]:
                dt = time.time() - _last["t"]
                rate = f" | {(msgs - _last['messages']) / dt:.0f} msg/s"
            _last["messages"], _last["t"] = msgs, time.time()
            if age > 15:
                log("WARN", f"decoder data STALE ({age:.0f}s old) — waiting for recovery")
            else:
                log("OK", f"aircraft {n} ({npos} w/pos){rate} | json age {age:.1f}s")
        except FileNotFoundError:
            log("WARN", "no aircraft.json yet — decoder not writing")
        except Exception as e:
            log("WARN", f"stats: {e}")


threading.Thread(target=stats_loop, daemon=True).start()


# ---- attach + decode loop ---------------------------------------------------
def run(cmd):
    """Run a command, streaming colorized output; returns exit code."""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, errors="replace")
    for line in p.stdout:
        raw(line)
    return p.wait()


def find_busid():
    """Locate the RTL stick by hardware id — survives USB port changes."""
    try:
        out = subprocess.run(["usbipd", "list"], capture_output=True,
                             text=True, timeout=10).stdout
        for line in out.splitlines():
            if VIDPID in line:
                return line.split()[0], ("Attached" in line), ("Shared" in line
                                                               or "Attached" in line)
    except Exception as e:
        log("ERR", f"usbipd list failed: {e}")
    return None, False, False


while True:
    busid, attached, bound = find_busid()
    if not busid:
        log("WARN", f"no RTL stick ({VIDPID}) on any USB port — plug it in; retry in 15 s")
        time.sleep(15)
        continue
    if not bound:
        log("WARN", f"stick at busid {busid} is NOT bound — run once as admin: "
                    f"usbipd bind --busid {busid}")
        time.sleep(15)
        continue
    if attached:
        log("INFO", f"stick at busid {busid} already attached to WSL")
    else:
        log("INFO", f"attaching stick (busid {busid}) to WSL…")
        rc = run(["usbipd", "attach", "--wsl", "--busid", busid])
        if rc == 0:
            log("INFO", "attach OK — starting decoder loop in WSL")
        else:
            log("WARN", f"attach rc={rc} (WSL cold-booting? decoder loop will verify)")
    rc = run(["wsl", "-d", "Ubuntu", "-u", "root", "--", "/opt/adsb/run.sh"])
    log("WARN", f"decoder loop ended rc={rc} — re-discover + restart in 10 s")
    time.sleep(10)
