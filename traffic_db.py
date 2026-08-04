# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 MaxBotev
"""
A record of where aircraft actually fly, for judging observing sites.

The question this exists to answer is "if I set up two miles that way, would I
see more transits?" -- and that cannot be answered from the az/el this engine
already computes, because az/el is relative to THIS site. Move the observer and
every angle changes. So what gets stored is the aircraft's own position in
space: latitude, longitude, altitude. Any candidate site can then be evaluated
by recomputing the geometry from there.

Storing raw tracks was never an option. Forty aircraft in range at 1 Hz is
3.5 million rows a day, and this runs on a Pi that also has to not miss a
transit. Instead each sample increments a counter in a coarse 4-D histogram --
latitude, longitude, altitude, time of day -- so the table saturates instead of
growing... except it does not, quite. Airliners repeat the same airways, but
each pass wanders within the corridor, so new bins keep appearing: simulated
over a month, 175k rows after one day became 1.5M after thirty, and 67 MB.

So bins expire. Every hit refreshes a row's timestamp, and rows untouched for
the retention window are dropped. A corridor flown daily never ages out; a
Cessna that pottered past once does, which is the right outcome twice over --
it bounds the table, and one-off traffic is unpredictable and should not
influence a site score anyway.

Bin size is a real trade-off rather than a detail. A bin subtends bin/range
radians at the observer, and the Moon is half a degree across, so 0.002 deg
(about 220 m) is roughly one lunar diameter at 15 km. Finer would multiply the
table for precision the input does not have -- ADS-B positions are good to tens
of metres and the traffic itself varies day to day. This is for ranking sites,
not for predicting an individual crossing.

Nothing here runs on the prediction loop's critical path: samples land in a
dict in memory, and a worker thread flushes them to SQLite once a minute.
"""

import math
import os
import sqlite3
import threading
import time

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "traffic.db")

DEFAULTS = {
    "traffic_log": False,          # master switch
    "traffic_max_km": 80.0,        # ignore contacts beyond this
    "traffic_min_alt_ft": 1500.0,  # ground clutter and circuits are not transits
    "traffic_bin_deg": 0.002,      # ~220 m; about one lunar diameter at 15 km
    "traffic_bin_ft": 500.0,
    "traffic_hours_per_bucket": 3,  # 8 buckets a day
    "traffic_split_weekend": True,  # keep weekday and weekend traffic apart
    "traffic_sample_s": 2.0,       # bin at most this often; an aircraft crosses
                                   # a 220 m bin in about a second at cruise, so
                                   # 1 Hz records almost every cell twice over
    "traffic_flush_s": 60.0,
    "traffic_retain_days": 7.0,    # bins unseen for this long are dropped
    "traffic_min_hits": 2,         # ...and so are one-off bins, at prune time
    "traffic_max_rows": 2000000,   # last-resort ceiling
    "traffic_prune_s": 1800.0,
}


def _conn(path=None):
    c = sqlite3.connect(path or DB_PATH, timeout=20.0)
    c.execute("PRAGMA journal_mode=WAL")     # never block a reader on a flush
    c.execute("PRAGMA synchronous=NORMAL")
    return c


def init(path=None):
    c = _conn(path)
    try:
        c.execute("""CREATE TABLE IF NOT EXISTS traffic (
                        latb INTEGER NOT NULL,
                        lonb INTEGER NOT NULL,
                        altb INTEGER NOT NULL,
                        tod  INTEGER NOT NULL,
                        dow  INTEGER NOT NULL DEFAULT 0,
                        n    INTEGER NOT NULL,
                        last_t INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (latb, lonb, altb, tod, dow))""")
        cols = [r[1] for r in c.execute("PRAGMA table_info(traffic)")]
        if "last_t" not in cols:                 # upgrade an older database
            c.execute("ALTER TABLE traffic ADD COLUMN last_t INTEGER NOT NULL DEFAULT 0")
        if "dow" not in cols:
            # dow belongs in the primary key, which SQLite cannot alter, so the
            # table is rebuilt. Existing rows keep their counts and land in the
            # weekday bucket -- imprecise for one window, and self-correcting
            # once a week of real data has come through.
            c.execute("ALTER TABLE traffic RENAME TO traffic_old")
            c.execute("""CREATE TABLE traffic (
                            latb INTEGER NOT NULL, lonb INTEGER NOT NULL,
                            altb INTEGER NOT NULL, tod INTEGER NOT NULL,
                            dow INTEGER NOT NULL DEFAULT 0, n INTEGER NOT NULL,
                            last_t INTEGER NOT NULL DEFAULT 0,
                            PRIMARY KEY (latb, lonb, altb, tod, dow))""")
            c.execute("INSERT INTO traffic (latb,lonb,altb,tod,dow,n,last_t) "
                      "SELECT latb,lonb,altb,tod,0,n,last_t FROM traffic_old")
            c.execute("DROP TABLE traffic_old")
        c.execute("CREATE INDEX IF NOT EXISTS traffic_last_t ON traffic(last_t)")
        c.execute("""CREATE TABLE IF NOT EXISTS meta (
                        k TEXT PRIMARY KEY, v TEXT)""")
        c.commit()
    finally:
        c.close()


class Recorder:
    """Accumulates in memory, writes in batches, never on the caller's thread."""

    def __init__(self, cfg, log, path=None):
        self.cfg = dict(DEFAULTS)
        for k in DEFAULTS:
            if k in cfg:
                self.cfg[k] = cfg[k]
        self.log = log
        self.path = path or DB_PATH
        self.pending = {}
        self.lock = threading.Lock()
        self.last_flush = 0.0
        self.samples = 0
        self.flushed = 0
        self.last_prune = 0.0
        self._last_sample = 0.0
        self.started = False

    # ---- ingest ----------------------------------------------------------
    def observe(self, aircraft, home_lat, home_lon, now):
        """Bin one tick's worth of contacts. Called from the prediction loop,
        so it does nothing but arithmetic and dict updates."""
        if not self.cfg.get("traffic_log"):
            return
        if now - getattr(self, "_last_sample", 0.0) < float(
                self.cfg.get("traffic_sample_s", 2.0)):
            return
        self._last_sample = now
        if not self.started:
            init(self.path)
            self.started = True
            self.log("traffic", "recording aircraft positions for site analysis "
                                "(bins %.4f deg / %.0f ft / %d h)"
                     % (self.cfg["traffic_bin_deg"], self.cfg["traffic_bin_ft"],
                        self.cfg["traffic_hours_per_bucket"]))

        bd = float(self.cfg["traffic_bin_deg"])
        bft = float(self.cfg["traffic_bin_ft"])
        maxkm = float(self.cfg["traffic_max_km"])
        minalt = float(self.cfg["traffic_min_alt_ft"])
        hpb = max(1, int(self.cfg["traffic_hours_per_bucket"]))
        # Local time of day: traffic patterns are diurnal, and the Moon is only
        # observable at some hours, so the two have to be matched up later.
        lt = time.localtime(now)
        tod = int(lt.tm_hour) // hpb
        # Weekday and weekend traffic are genuinely different -- fewer airline
        # departures, more light aircraft -- and a site is usually judged for
        # one or the other. Two buckets doubles the table at worst; per-DATE
        # bins would multiply it by the whole retention window, for a
        # distinction nobody plans an observing trip around.
        dow = 1 if (self.cfg.get("traffic_split_weekend", True)
                    and lt.tm_wday >= 5) else 0

        coslat = math.cos(math.radians(home_lat))
        add = {}
        for a in aircraft:
            alt = a.get("alt_ft")
            if not isinstance(alt, (int, float)) or alt < minalt:
                continue
            dlat = a["lat"] - home_lat
            dlon = (a["lon"] - home_lon) * coslat
            if math.hypot(dlat, dlon) * 111.32 > maxkm:
                continue
            key = (int(math.floor(a["lat"] / bd)),
                   int(math.floor(a["lon"] / bd)),
                   int(math.floor(alt / bft)),
                   tod, dow)
            add[key] = add.get(key, 0) + 1
        if not add:
            return
        with self.lock:
            for k, v in add.items():
                self.pending[k] = self.pending.get(k, 0) + v
            self.samples += sum(add.values())

    def due(self, now):
        return (self.cfg.get("traffic_log") and self.pending
                and now - self.last_flush >= float(self.cfg["traffic_flush_s"]))

    # ---- persist ---------------------------------------------------------
    def flush(self, now):
        with self.lock:
            batch, self.pending = self.pending, {}
            self.last_flush = now
        if not batch:
            return
        try:
            c = _conn(self.path)
            try:
                ts = int(now)
                c.executemany(
                    "INSERT INTO traffic (latb,lonb,altb,tod,dow,n,last_t) "
                    "VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT(latb,lonb,altb,tod,dow) DO UPDATE SET "
                    "n = n + excluded.n, last_t = excluded.last_t",
                    [(k[0], k[1], k[2], k[3], k[4], v, ts)
                     for k, v in batch.items()])
                c.commit()
                self.flushed += len(batch)
                if now - self.last_prune >= float(self.cfg["traffic_prune_s"]):
                    self.last_prune = now
                    self._prune(c, now)
            finally:
                c.close()
        except Exception as e:
            self.log("traffic", "could not write the traffic database: %s" % e,
                     ok=False)

    def _prune(self, c, now):
        """Age out what is no longer being flown, then anything one-off.

        A bin that keeps being hit keeps its timestamp refreshed, so the busy
        airways are immortal and everything else decays. That is what bounds
        the table -- and dropping bins seen once or twice also drops exactly
        the traffic worth ignoring, since a light aircraft that wandered
        through last Tuesday says nothing about what a site will see tonight.
        """
        before = c.execute("SELECT COUNT(*) FROM traffic").fetchone()[0]
        cutoff = int(now - float(self.cfg["traffic_retain_days"]) * 86400.0)
        c.execute("DELETE FROM traffic WHERE last_t < ?", (cutoff,))
        c.execute("DELETE FROM traffic WHERE last_t < ? AND n < ?",
                  (int(now - 86400.0), int(self.cfg["traffic_min_hits"])))
        cap = int(self.cfg["traffic_max_rows"])
        n = c.execute("SELECT COUNT(*) FROM traffic").fetchone()[0]
        if n > cap:
            c.execute("DELETE FROM traffic WHERE rowid IN ("
                      "SELECT rowid FROM traffic ORDER BY n ASC LIMIT ?)", (n - cap,))
        c.commit()
        after = c.execute("SELECT COUNT(*) FROM traffic").fetchone()[0]
        if before != after:
            self.log("traffic", "pruned %d expired bins (%d -> %d rows, keeping "
                                "%.0f days)" % (before - after, before, after,
                                                self.cfg["traffic_retain_days"]))

    def stats(self, now):
        out = {"enabled": bool(self.cfg.get("traffic_log")),
               "samples_this_run": self.samples, "pending": len(self.pending)}
        try:
            c = _conn(self.path)
            try:
                out["rows"] = c.execute("SELECT COUNT(*) FROM traffic").fetchone()[0]
                out["observations"] = (c.execute(
                    "SELECT COALESCE(SUM(n),0) FROM traffic").fetchone()[0])
            finally:
                c.close()
            out["bytes"] = os.path.getsize(self.path)
        except Exception as e:
            out["error"] = str(e)
        return out


def _filter(tod, dow):
    where, args = [], []
    if tod is not None and tod != "":
        where.append("tod = ?")
        args.append(int(tod))
    if dow is not None and dow != "":
        where.append("dow = ?")
        args.append(int(dow))
    return ((" WHERE " + " AND ".join(where)) if where else ""), args


def load_bins(path=None, tod=None, dow=None):
    """Every occupied bin as (latb, lonb, altb, tod, n) for the scorer."""
    c = _conn(path)
    try:
        w, args = _filter(tod, dow)
        return c.execute("SELECT latb,lonb,altb,tod,n FROM traffic" + w,
                         args).fetchall()
    finally:
        c.close()


def cloud(path=None, tod=None, dow=None, coarsen=5, alt_coarsen=4, limit=40000):
    """Traffic as a point cloud for display, re-binned coarser.

    The stored grid is far finer than a browser wants to draw -- hundreds of
    thousands of bins, most of them one pixel apart at any sane zoom. Merging
    them down loses nothing visible and keeps the payload sane. Busiest first,
    so a cap trims the faint edges rather than an arbitrary corner of the map.
    """
    c = _conn(path)
    try:
        w, args = _filter(tod, dow)
        q = ("SELECT latb/?*?, lonb/?*?, altb/?*?, SUM(n) AS s FROM traffic" + w +
             " GROUP BY 1,2,3 ORDER BY s DESC LIMIT ?")
        p = [coarsen, coarsen, coarsen, coarsen, alt_coarsen, alt_coarsen]
        return c.execute(q, p + args + [int(limit)]).fetchall()
    finally:
        c.close()


def buckets(path=None):
    """Which (tod, dow) combinations actually hold data, and how much."""
    c = _conn(path)
    try:
        return c.execute("SELECT tod, dow, COUNT(*), SUM(n) FROM traffic "
                         "GROUP BY tod, dow ORDER BY tod, dow").fetchall()
    finally:
        c.close()
