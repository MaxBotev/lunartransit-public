# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 MaxBotev
"""
Spiral search: acquire the Moon when a session-start GOTO misses the field.

Why this is needed at all: with imperfect polar alignment plus cone error, a
cold GOTO can land degrees away, and at lunar exposures there are no stars to
plate-solve against. Measured on this rig before the first sync of the night:
2.7 degrees of pointing error into a field 0.84 degrees tall. The Moon simply
is not there, and no amount of closed-loop centring helps because the centring
loop needs to SEE the disc first.

So: walk an outward square spiral of overlapping pointings around the Moon's
ephemeris position, snapping one frame per tile, until the disc appears. Then
hand off to the existing centring loop and SYNC, after which the mount's model
is corrected and no further searching is needed for the rest of the night on
that side of the pier.

Two details that matter:

  * The Moon moves ~35 arcsec/min, and a full search can run for many minutes.
    Every tile is therefore computed from a FRESH ephemeris position plus the
    tile offset, not from a position frozen at the start.

  * Tiles are commanded as absolute coordinates (SLEW), never as accumulated
    relative nudges, so a slew the mount under-delivers does not smear the
    whole grid.

Field of view is deliberately NOT assumed. The step size is configuration; on
a successful find the true field is measured from the lunar disc and reported,
so the step can be tuned from real numbers instead of a spec sheet.
"""

import math
import threading
import time

from meridian_flip import (FlipError, Flipper, _kv, _talk, load_bias,
                           save_bias)

DEFAULTS = {
    "search_step_deg": 0.6,      # tile spacing; keep under the SHORT field axis
    "search_radius_deg": 3.0,    # how far out to give up
    "search_max_s": 1500.0,      # hard budget; must exceed step/radius x tile time
    "search_settle_s": 1.5,      # let the mount stop ringing before snapping
    "search_centre_on_find": True,
    "search_sync_on_find": True,  # the whole point: never search twice a night
    # A blob is only believed to be the Moon if it is big enough and round
    # enough. Stars, hot-pixel clusters and amp glow all fail these.
    "search_min_blob_frac": 0.02,   # of the frame's SHORT axis, in pixels
    "search_max_aspect": 3.0,       # long/short bbox ratio (a crescent is ~2)
}

# MOONPOS says "no disc" in these words when the field is simply empty. Any
# other error (camera not live, capture wrote nothing) is a real fault and must
# abort the search rather than being silently counted as "Moon not here".
_EMPTY = ("no disc", "lit samples", "no samples")


def _is_empty_field(msg):
    m = msg.lower()
    return any(s in m for s in _EMPTY)


def _square_spiral(step_deg, radius_deg):
    """Tile offsets (dx, dy) in degrees, centre first, spiralling outward.

    A square spiral rather than an Archimedean one because the frame is a
    rectangle: rings of a square grid tile it without gaps, and each ring is
    fully searched before moving further out, so the common case (small error)
    finishes in the first few frames.
    """
    yield (0.0, 0.0)
    ring = 1
    while ring * step_deg <= radius_deg + 0.5 * step_deg:
        cells = []
        r = ring
        for x in range(-r, r + 1):          # top and bottom edges
            cells.append((x, r))
            cells.append((x, -r))
        for y in range(-r + 1, r):          # left and right edges
            cells.append((r, y))
            cells.append((-r, y))
        # Nearest-first inside the ring: the corners are the least likely spots
        # and cost the most slew time to reach.
        cells.sort(key=lambda c: c[0] * c[0] + c[1] * c[1])
        for cx, cy in cells:
            dx, dy = cx * step_deg, cy * step_deg
            if math.hypot(dx, dy) <= radius_deg + 1e-9:
                yield (dx, dy)
        ring += 1


class Finder(Flipper):
    """One acquisition attempt. Reuses Flipper's measure/calibrate/centre."""

    def __init__(self, cfg, engine, log):
        Flipper.__init__(self, cfg, engine, log)
        for k in DEFAULTS:
            self.cfg[k] = cfg.get(k, DEFAULTS[k])
        self.tiles_tried = 0

    def say(self, text, **kw):
        self.log("find", text, **kw)

    # ---- one tile --------------------------------------------------------
    def _goto_offset(self, dx_deg, dy_deg):
        """Point at (Moon now + tile offset). Dec offset is plain degrees; the
        RA offset is divided by cos(dec) so a tile is the same ON-SKY size at
        any declination -- without that, tiles near the pole overlap heavily
        and tiles near the equator leave gaps."""
        ra_h, dec = self.moon_radec(time.time() + 8.0)   # ~slew lead
        dec_t = max(-89.0, min(89.0, dec + dy_deg))
        cosd = math.cos(math.radians(max(-89.0, min(89.0, dec))))
        ra_t = (ra_h + (dx_deg / max(cosd, 1e-3)) / 15.0) % 24.0
        self.cmd("SLEW %.6f %.6f" % (ra_t, dec_t))
        time.sleep(float(self.cfg["search_settle_s"]))

    def _look(self):
        """Snap and classify. Returns a disc tuple, or None for an empty field.

        Raises on anything that is not "the Moon is elsewhere", because a dead
        camera would otherwise look exactly like a sky with no Moon in it and
        the search would grind through every tile before failing.
        """
        try:
            reply = self.cmd("MOONPOS", timeout=120.0)
        except FlipError as e:
            if _is_empty_field(str(e)):
                return None
            raise
        d = _kv(reply)
        x0, y0 = float(d["bx0"]), float(d["by0"])
        x1, y1 = float(d["bx1"]), float(d["by1"])
        w, h = float(d["w"]), float(d["h"])
        long_side = max(x1 - x0, y1 - y0)
        short_side = max(1.0, min(x1 - x0, y1 - y0))
        if long_side < float(self.cfg["search_min_blob_frac"]) * min(w, h):
            return None                     # a star or a hot pixel, not a disc
        touches = (x0 <= 1 or y0 <= 1 or x1 >= w - 2 or y1 >= h - 2)
        # A disc hanging off the frame edge is legitimately elongated, so the
        # roundness test only applies to blobs that are wholly inside.
        if not touches and long_side / short_side > float(self.cfg["search_max_aspect"]):
            return None                     # a streak or a gradient, not a disc
        return {"cx": 0.5 * (x0 + x1), "cy": 0.5 * (y0 + y1),
                "diam": long_side, "w": w, "h": h, "clipped": touches,
                # how far off frame centre, as a fraction of the half-frame:
                # used to choose the best of several detections
                "off": math.hypot((0.5 * (x0 + x1) - w / 2.0) / (w / 2.0),
                                  (0.5 * (y0 + y1) - h / 2.0) / (h / 2.0))}

    # ---- the search ------------------------------------------------------
    def run(self):
        # Refuse outright if the target is below the working limit. A spiral
        # search aimed under the horizon would walk the OTA down towards the
        # ground and the tripod; the mount's own limits may catch it, but that
        # is its last line of defence, not this code's first.
        floor = float(self.cfg.get("lunar_min_elev_deg", 10.0))
        el = None
        try:
            el = ((self.engine.snapshot() or {}).get("moon") or {}).get("el")
        except Exception:
            pass
        if el is None:
            raise FlipError("cannot read the Moon's elevation — refusing to "
                            "slew blind")
        if el < floor:
            raise FlipError("the Moon is at %.1f deg elevation, below the %.0f "
                            "deg working limit — nothing up there to find"
                            % (el, floor))

        st = _kv(self.cmd("MOUNT"))
        if str(st.get("parked", "")).lower() == "true":
            raise FlipError("mount is parked — unpark before searching")
        pier0 = st.get("side") or st.get("pier", "?")
        step = float(self.cfg["search_step_deg"])
        radius = float(self.cfg["search_radius_deg"])
        deadline = time.time() + float(self.cfg["search_max_s"])
        tiles = list(_square_spiral(step, radius))
        self.say("searching for the Moon: up to %d pointings, %.2f deg apart, "
                 "out to %.1f deg (pier %s)" % (len(tiles), step, radius, pier0))

        best = None
        queue = list(tiles)
        bias_done = False
        while queue:
            dx, dy = queue.pop(0)
            if time.time() > deadline:
                raise FlipError("search gave up after %.0f min without finding "
                                "the Moon — check the mount is unparked, the "
                                "camera is live, and the lens cap is off"
                                % (float(self.cfg["search_max_s"]) / 60.0))
            self._goto_offset(dx, dy)
            self.tiles_tried += 1

            # A search must never be the thing that swings the OTA through the
            # mount. If a tile slew changed pier side, something is wrong with
            # the geometry assumptions -- stop and hand back to the operator.
            side = (_kv(self.cmd("MOUNT")).get("side") or "?")
            if pier0 == "?" and side != "?":
                # At the home position (Dec ~90) pier side is geometrically
                # meaningless and the mount reports it as unknown. The first
                # tile slew leaves home, so latch the real side there and
                # guard every tile after it.
                pier0 = side
                self.say("pier side is %s after the first pointing" % side)
            elif pier0 != "?" and side != "?" and side != pier0:
                raise FlipError("a search tile changed pier side (%s -> %s) — "
                                "stopped; the Moon is at the meridian, run the "
                                "flip instead" % (pier0, side))

            # The pier side is only knowable once the mount has left home, so
            # the remembered bias can only be applied from the second pointing
            # on. It goes to the FRONT of the queue: if the mount's error has
            # repeated -- and across two nights it repeated to 0.175 deg -- the
            # Moon is there, and the other 80 tiles are never visited.
            if not bias_done and pier0 != "?":
                bias_done = True
                b = load_bias(pier0)
                if b:
                    queue.insert(0, b)
                    self.say("trying the remembered pointing bias for pier %s "
                             "first: %+.2f, %+.2f deg" % (pier0, b[0], b[1]))

            hit = self._look()
            if hit is None:
                continue
            self.say("disc seen at tile %+.2f,%+.2f deg (%d frame%s in)%s"
                     % (dx, dy, self.tiles_tried,
                        "" if self.tiles_tried == 1 else "s",
                        " — on the frame edge, pulling in" if hit["clipped"] else ""))
            best = (dx, dy, hit)
            if hit["clipped"]:
                hit = self._pull_in() or hit
                best = (dx, dy, hit)
            break

        if best is None:
            raise FlipError("searched %d pointings out to %.1f deg and the Moon "
                            "was in none of them" % (self.tiles_tried, radius))

        dx, dy, hit = best
        # Report the field in real units so the step can be tuned from
        # measurement rather than from a datasheet.
        scale = self.engine.moon_angular_diameter_arcsec() / hit["diam"]
        fov_w, fov_h = hit["w"] * scale / 3600.0, hit["h"] * scale / 3600.0
        self.say("Moon acquired %.2f deg from the ephemeris position after %d "
                 "frames — field is %.2f x %.2f deg at %.2f arcsec/px"
                 % (math.hypot(dx, dy), self.tiles_tried, fov_w, fov_h, scale),
                 tiles=self.tiles_tried, offset_deg=round(math.hypot(dx, dy), 3),
                 fov_deg=[round(fov_w, 3), round(fov_h, 3)])
        if step > 0.9 * min(fov_w, fov_h):
            self.say("note: search_step_deg (%.2f) is larger than 90%% of the "
                     "short field axis (%.2f) — tiles may leave gaps; consider "
                     "%.2f" % (step, min(fov_w, fov_h), 0.75 * min(fov_w, fov_h)))

        out = {"ok": True, "tiles": self.tiles_tried,
               "offset_deg": round(math.hypot(dx, dy), 3),
               "fov_deg": [round(fov_w, 3), round(fov_h, 3)],
               "arcsec_per_px": round(scale, 3)}

        if self.cfg.get("search_centre_on_find", True):
            out["residual_arcsec"] = round(self.centre(), 1)
        if self.cfg.get("search_sync_on_find", True):
            # This is the payoff: one sync on a genuinely centred Moon turns a
            # multi-degree model error into arcminutes, so the next GOTO on
            # this pier side lands and no search is needed again tonight.
            try:
                ra, dec = self.moon_radec(time.time())
                # Read the model's belief BEFORE correcting it: the difference
                # is the bias worth remembering for the next re-home.
                st = _kv(self.cmd("MOUNT"))
                self.say(self.cmd("SYNC %.6f %.6f" % (ra, dec),
                                  timeout=60.0)[3:].strip())
                out["synced"] = True
                try:
                    cosd = math.cos(math.radians(dec))
                    save_bias(st.get("side") or pier0,
                              (float(st["ra"]) - ra) * 15.0 * cosd,
                              float(st["dec"]) - dec, self.log)
                except Exception:
                    pass
            except FlipError as e:
                self.say("WARNING: sync after acquisition failed (%s) — the "
                         "Moon is centred but the pointing model is not fixed,"
                         " so the next GOTO will miss again" % e)
                out["synced"] = False
        return out

    # ---- pulling a half-visible disc into the frame ----------------------
    #
    # The ordinary centring loop cannot start from a clipped disc: the bounding
    # box's centre is not the disc's centre, and its width is not the disc's
    # diameter, so both the measured error AND the arcsec-per-pixel ruler are
    # wrong. Feeding that to the loop makes it oscillate and diverge.
    #
    # But a clipped disc still carries enough information. The Moon is smaller
    # than the frame in every axis, so it can never touch BOTH edges of an
    # axis at once -- at least one edge of each axis is always a genuine limb
    # point. Those free edges are used for everything here: to learn the camera
    # rotation (they translate rigidly with the mount) and to reconstruct the
    # true centre (free edge, minus a radius). One or two nudges later the disc
    # is wholly inside and the normal loop takes over with valid measurements.

    def _edges(self, soft=False):
        try:
            d = _kv(self.cmd("MOONPOS", timeout=120.0))
        except FlipError as e:
            if soft and _is_empty_field(str(e)):
                return None          # probe pushed the sliver out of frame
            raise
        return dict((k, float(d[k])) for k in
                    ("bx0", "by0", "bx1", "by1", "w", "h"))

    @staticmethod
    def _free(e):
        return {"x0": e["bx0"] > 1, "x1": e["bx1"] < e["w"] - 2,
                "y0": e["by0"] > 1, "y1": e["by1"] < e["h"] - 2}

    @classmethod
    def _shift(cls, a, b):
        """Pixel displacement between two frames, from edges free in BOTH."""
        fa, fb = cls._free(a), cls._free(b)
        dxs = [b["b" + k] - a["b" + k] for k in ("x0", "x1") if fa[k] and fb[k]]
        dys = [b["b" + k] - a["b" + k] for k in ("y0", "y1") if fa[k] and fb[k]]
        if not dxs or not dys:
            return None
        return sum(dxs) / len(dxs), sum(dys) / len(dys)

    def _calibrate_clipped(self):
        """Learn pixel <- (dRA, dDec) using only un-clipped bounding-box edges."""
        step = float(self.cfg["calib_step_arcsec"])
        base = self._edges()
        probes = {}
        for axis, c in (("ra", "NUDGE %.1f 0"), ("dec", "NUDGE 0 %.1f")):
            # Probing outward from an already half-visible disc can push it off
            # the frame entirely. If that happens, back up and probe the other
            # way instead -- the mapping is linear, so the sign just flips.
            got = None
            for sign in (1.0, -1.0):
                self.cmd(c % (sign * step))
                m = self._edges(soft=True)
                s = self._shift(base, m) if m else None
                self.cmd(c % (-sign * step))
                if s:
                    got = (sign * s[0], sign * s[1])
                    break
            if got is None:
                raise FlipError(
                    "cannot calibrate from a clipped disc: neither %s probe "
                    "left a bounding-box edge free in both frames"
                    % axis.upper())
            probes[axis] = got
        (dx_ra, dy_ra), (dx_dec, dy_dec) = probes["ra"], probes["dec"]
        dx_ra, dy_ra = dx_ra / step, dy_ra / step
        dx_dec, dy_dec = dx_dec / step, dy_dec / step
        det = dx_ra * dy_dec - dx_dec * dy_ra
        if abs(det) < 1e-9:
            raise FlipError("clipped-edge calibration degenerate (det=%.2e) — "
                            "did the mount ignore the probe slews?" % det)
        self.rot = (dy_dec / det, -dx_dec / det, -dy_ra / det, dx_ra / det)
        self.say("acquisition calibrated from free limb edges: 1 arcsec RA = "
                 "(%.3f,%.3f) px, 1 arcsec Dec = (%.3f,%.3f) px"
                 % (dx_ra, dy_ra, dx_dec, dy_dec))

    @staticmethod
    def _reconstruct(e):
        """Best estimate of the disc centre when the box may be clipped.

        The radius comes from the largest FULLY free extent, which is a true
        chord; on a clipped axis the centre is that free edge offset inward by
        one radius.
        """
        f = Finder._free(e)
        ext_x, ext_y = e["bx1"] - e["bx0"], e["by1"] - e["by0"]
        full = [ext for ext, ok in ((ext_x, f["x0"] and f["x1"]),
                                    (ext_y, f["y0"] and f["y1"])) if ok]
        r = 0.5 * (max(full) if full else max(ext_x, ext_y))
        cx = (0.5 * (e["bx0"] + e["bx1"]) if f["x0"] and f["x1"]
              else (e["bx0"] + r if f["x0"] else e["bx1"] - r))
        cy = (0.5 * (e["by0"] + e["by1"]) if f["y0"] and f["y1"]
              else (e["by0"] + r if f["y0"] else e["by1"] - r))
        return cx, cy

    def _pull_in(self, max_iter=4):
        """Nudge a half-visible disc wholly into the frame. Returns a clean
        measurement, or None if it could not be freed."""
        for i in range(max_iter):
            e = self._edges()
            f = self._free(e)
            if all(f.values()):
                self.say("disc pulled clear of the frame edges after %d nudge%s"
                         % (i, "" if i == 1 else "s"))
                return {"cx": 0.5 * (e["bx0"] + e["bx1"]),
                        "cy": 0.5 * (e["by0"] + e["by1"]),
                        "diam": max(e["bx1"] - e["bx0"], e["by1"] - e["by0"]),
                        "w": e["w"], "h": e["h"], "clipped": False, "off": 0.0}
            if self.rot is None:
                self._calibrate_clipped()
            cx, cy = self._reconstruct(e)
            dra, ddec = self.pixel_to_correction(cx - e["w"] / 2.0,
                                                 cy - e["h"] / 2.0)
            self.say("disc is off the %s edge — pulling in (dRA=%.0f dDec=%.0f)"
                     % ("/".join(k for k in ("x0", "x1", "y0", "y1") if not f[k]),
                        dra, ddec))
            self.cmd("NUDGE %.1f %.1f" % (dra, ddec))
        self.say("WARNING: the disc is still on a frame edge after %d nudges — "
                 "handing to the centring loop anyway" % max_iter)
        return None


def start(cfg, engine, log, on_done=None):
    """Run a search on a worker thread so the prediction loop keeps running."""
    box = {}

    def _worker():
        try:
            box["result"] = Finder(cfg, engine, log).run()
        except Exception as e:
            log("find", "MOON SEARCH FAILED: %s — mount left where it is"
                % e, ok=False)
            box["result"] = {"ok": False, "info": str(e)}
        if on_done:
            on_done(box["result"])

    t = threading.Thread(target=_worker, name="moon-find", daemon=True)
    t.start()
    return t, box
