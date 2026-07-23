# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 MaxBotev
"""
Classify an ADS-B contact into a display category for the map.

Signals used:
  * dump1090 emitter `category` (A7 = rotorcraft, A6 = high-performance, B1 glider,
    B2 balloon, B6 UAV, C* = ground vehicles/obstructions).
  * military ICAO hex-address ranges (readsb/tar1090 `isMilRange` list).
  * adsbdb registered owner string (matches "air force", "navy", ... ).
  * ICAO type designator as a fallback helicopter check.

Returns one of:
  plane, helicopter, military, mil_helicopter, glider, balloon, drone, ground
"""

# Military ICAO 24-bit address ranges (from readsb isMilRange, dev branch).
MIL_RANGES = [
    (0xadf7c8, 0xafffff), (0x010070, 0x01008f), (0x0a4000, 0x0a4fff),
    (0x33ff00, 0x33ffff), (0x350000, 0x37ffff), (0x3aa000, 0x3affff),
    (0x3b7000, 0x3bffff), (0x3ea000, 0x3ebfff), (0x3f4000, 0x3fbfff),
    (0x400000, 0x40003f), (0x43c000, 0x43cfff), (0x444000, 0x446fff),
    (0x44f000, 0x44ffff), (0x457000, 0x457fff), (0x45f400, 0x45f4ff),
    (0x468000, 0x4683ff), (0x473c00, 0x473c0f), (0x478100, 0x4781ff),
    (0x480000, 0x480fff), (0x48d800, 0x48d87f), (0x497c00, 0x497cff),
    (0x498420, 0x49842f), (0x4b7000, 0x4b7fff), (0x4b8200, 0x4b82ff),
    (0x70c070, 0x70c07f), (0x710258, 0x71028f), (0x710380, 0x71039f),
    (0x738a00, 0x738aff), (0x7cf800, 0x7cfaff), (0x800200, 0x8002ff),
    (0xc20000, 0xc3ffff), (0xe40000, 0xe41fff),
]

MIL_OWNER_KEYWORDS = (
    "air force", "navy", "army", "marine corps", "national guard",
    "ministry of defence", "ministry of defense", "department of defense",
    "royal air force", "royal navy", "luftwaffe", "armee", "armée",
    "military", "us air", "usaf", "usmc", "raf ", "naval",
)

# Common helicopter ICAO type designators (fallback when category is missing).
HELO_TYPES = {
    "EC30", "EC35", "EC45", "EC20", "EC25", "EC55", "EC75",
    "H125", "H130", "H135", "H145", "H160", "H175", "H500",
    "AS50", "AS55", "AS65", "A109", "A119", "A139", "A149", "A169", "A189",
    "B06", "B407", "B412", "B429", "B430", "B505", "B47G",
    "R22", "R44", "R66", "S76", "S70", "S92", "UH60", "H60", "BK17",
    "EH10", "AW09", "AW39", "AW69", "AW89", "MD52", "MD60", "MI8", "MI17",
    "CH47", "V22", "H64", "AH64", "OH58", "EXPL", "GAZL", "LYNX", "PUMA",
}


def is_mil_hex(hexid):
    try:
        v = int(hexid, 16)
    except (TypeError, ValueError):
        return False
    return any(lo <= v <= hi for lo, hi in MIL_RANGES)


def is_mil_owner(owner):
    if not owner:
        return False
    o = owner.lower()
    return any(k in o for k in MIL_OWNER_KEYWORDS)


def classify(category=None, hexid=None, owner=None, icao_type=None,
             ac_class=None, engine=None):
    cat = (category or "").upper()
    t = (icao_type or "").upper()
    cls = (ac_class or "").upper()      # Doc 8643 airframe class: L/S/A/H/G/T
    eng = (engine or "").upper()        # Doc 8643 engine: P/T/J/E/R

    military = is_mil_hex(hexid) or is_mil_owner(owner)
    # Doc 8643 class is the most reliable signal; emitter category + type set back it up.
    helo = cls in ("H", "G") or cat == "A7" or t in HELO_TYPES
    fixed_wing = cls in ("L", "S", "A")
    # prop / light: piston or turboprop fixed-wing (Cessna, Piper, King Air, PC-12...)
    prop = fixed_wing and eng in ("P", "T")

    if cat.startswith("C"):          # surface vehicle / obstruction
        return "ground"
    if helo:
        return "mil_helicopter" if military else "helicopter"
    if military:
        return "military"
    if cat == "B1":
        return "glider"
    if cat == "B2":
        return "balloon"
    if cat == "B6":
        return "drone"
    if prop:
        return "prop"
    return "plane"
