# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 MaxBotev
"""
Aircraft enrichment with on-disk caching, filled by a background worker so the
UI never blocks.

  * route(callsign) -> airline + origin/destination airports.
      Primary source: FlightRadar24's live search feed (accurate for the SPECIFIC
      flight currently airborne, incl. city names). Fallback: adsbdb.com.
      NOTE: flight numbers get reused for different city pairs, so the static
      callsign->route databases (adsbdb/hexdb) are often stale/wrong. FR24
      reflects the live flight, matching what you see on flightradar24.com.
      (FR24's endpoint is unofficial — fine for a personal station.)
  * aircraft(hex) -> ICAO type, registration, registered owner (adsbdb).

Routes are cached for a few hours (a callsign maps to TODAY's flight); aircraft
type/reg is cached for a week.
"""

import json
import os
import queue
import re
import threading
import time
import urllib.parse
import urllib.request

CACHE_PATH = os.path.join(os.path.dirname(__file__), "route_cache.json")
CALLSIGN_API = "https://api.adsbdb.com/v0/callsign/"
AIRCRAFT_API = "https://api.adsbdb.com/v0/aircraft/"
FR24_FIND = "https://www.flightradar24.com/v1/search/web/find?limit=20&query="
FR24_UA = ("Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/120 Safari/537.36")
NEG_TTL = 1800              # retry unknowns after 30 min
ROUTE_POS_TTL = 3 * 3600   # routes are time-sensitive (callsign reused daily)
AC_POS_TTL = 7 * 86400     # aircraft type/reg is stable
USER_AGENT = "UFOMonitor/1.0 (personal ADS-B station)"
ARROW = "⟶"           # FR24 route separator: ⟶


def _airport(iata=None, icao=None, name=None, city=None, country=None):
    return {"iata": iata, "icao": icao, "name": name, "city": city, "country": country}


def _airport_from_adsbdb(x):
    if not x:
        return None
    return _airport(x.get("iata_code"), x.get("icao_code"), x.get("name"),
                    x.get("municipality"), x.get("country_iso_name"))


def _parse_fr24_route(route_str, from_code, to_code):
    """'San Francisco (SFO) ⟶ Las Vegas (LAS)' -> (origin, destination)."""
    o = _airport(iata=from_code)
    d = _airport(iata=to_code)
    if route_str and ARROW in route_str:
        a, _, b = route_str.partition(ARROW)
        for side, ap in ((a, o), (b, d)):
            m = re.match(r"^\s*(.*?)\s*\(([A-Za-z0-9]{3,4})\)\s*$", side)
            if m:
                ap["city"] = m.group(1) or None
                ap["iata"] = m.group(2)
    return o, d


def _fetch_fr24_route(callsign):
    url = FR24_FIND + urllib.parse.quote(callsign)
    req = urllib.request.Request(url, headers={"User-Agent": FR24_UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        j = json.loads(r.read().decode())
    cu = callsign.upper()
    best = None
    for res in j.get("results", []):
        det = res.get("detail") or {}
        if not (det.get("schd_from") and det.get("schd_to")):
            continue
        if (det.get("callsign") or "").upper() == cu:
            best = det
            if res.get("type") == "live":
                break
    if best is None:
        return None
    o, d = _parse_fr24_route(best.get("route"), best.get("schd_from"), best.get("schd_to"))
    return {"airline": None, "airline_iata": None,
            "origin": o, "destination": d, "source": "fr24"}


def _fetch_adsbdb_route(callsign):
    req = urllib.request.Request(CALLSIGN_API + callsign, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=8) as r:
        j = json.loads(r.read().decode())
    fr = (j.get("response") or {})
    fr = fr.get("flightroute") if isinstance(fr, dict) else None
    if not fr:
        return None
    air = fr.get("airline") or {}
    return {"airline": air.get("name"), "airline_iata": air.get("iata"),
            "origin": _airport_from_adsbdb(fr.get("origin")),
            "destination": _airport_from_adsbdb(fr.get("destination")),
            "source": "adsbdb"}


def _fetch_route(callsign):
    try:
        r = _fetch_fr24_route(callsign)
        if r:
            return r
    except Exception:
        pass
    try:
        return _fetch_adsbdb_route(callsign)
    except Exception:
        return None


def _fetch_aircraft(hexid):
    req = urllib.request.Request(AIRCRAFT_API + hexid, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=8) as r:
        j = json.loads(r.read().decode())
    ac = (j.get("response") or {})
    ac = ac.get("aircraft") if isinstance(ac, dict) else None
    if not ac:
        return None
    return {"icao_type": ac.get("icao_type"), "type": ac.get("type"),
            "registration": ac.get("registration"),
            "owner": ac.get("registered_owner"),
            "owner_country": ac.get("registered_owner_country_name")}


class RouteCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.routes = {}        # callsign -> {"val": {...}|None, "ts": epoch}
        self.aircraft_db = {}   # hex      -> {"val": {...}|None, "ts": epoch}
        self.inflight = set()
        self.q = queue.Queue()
        self._load()
        threading.Thread(target=self._worker, daemon=True).start()

    def _load(self):
        try:
            with open(CACHE_PATH) as f:
                blob = json.load(f)
            self.routes = blob.get("routes", {})
            self.aircraft_db = blob.get("aircraft", {})
        except Exception:
            self.routes, self.aircraft_db = {}, {}

    def _save(self):
        try:
            tmp = CACHE_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"routes": self.routes, "aircraft": self.aircraft_db}, f)
            os.replace(tmp, CACHE_PATH)
        except Exception:
            pass

    def _get(self, store, key, kind, pos_ttl):
        now = time.time()
        with self.lock:
            e = store.get(key)
            if e is not None:
                ttl = pos_ttl if e["val"] else NEG_TTL
                if now - e["ts"] < ttl:
                    return e["val"]
            jobkey = (kind, key)
            if jobkey not in self.inflight:
                self.inflight.add(jobkey)
                self.q.put(jobkey)
            return e["val"] if e else None

    def route(self, callsign):
        if not callsign:
            return None
        return self._get(self.routes, callsign.strip().upper(), "route", ROUTE_POS_TTL)

    def aircraft(self, hexid):
        if not hexid:
            return None
        return self._get(self.aircraft_db, hexid.strip().lower(), "ac", AC_POS_TTL)

    def _worker(self):
        while True:
            kind, key = self.q.get()
            try:
                val = _fetch_route(key) if kind == "route" else _fetch_aircraft(key)
            except Exception:
                val = None
            store = self.routes if kind == "route" else self.aircraft_db
            with self.lock:
                store[key] = {"val": val, "ts": time.time()}
                self.inflight.discard((kind, key))
                self._save()
            time.sleep(0.3)  # be polite to the upstream services
