# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 MaxBotev
"""
ICAO Doc 8643 type-designator lookup.

Loads the compact `doc8643.json` (built from the public Doc 8643 dump):
    { "<DESIGNATOR>": {"mfr": "Cessna", "cls": "L", "model": "172"}, ... }

`cls` is the first letter of the Doc 8643 description code:
    L=LandPlane  S=SeaPlane  A=Amphibian  H=Helicopter  G=Gyrocopter  T=Tiltrotor

Used to (a) turn "C172" into a friendly "Cessna 172S" and (b) give the classifier
a reliable airframe class.
"""

import json
import os
import re

_DB = {}
_PATH = os.path.join(os.path.dirname(__file__), "doc8643.json")
try:
    with open(_PATH) as f:
        _DB = json.load(f)
except Exception as e:  # missing file -> graceful no-op
    print(f"[aircraft_types] could not load {_PATH}: {e}")


def lookup(designator):
    if not designator:
        return None
    return _DB.get(designator.strip().upper())


def aircraft_class(designator):
    """Return L/S/A/H/G/T (airframe class) or None."""
    info = lookup(designator)
    return info.get("cls") if info else None


def engine_type(designator):
    """Return P (piston), T (turbine/turboprop), J (jet), E, R, or None."""
    info = lookup(designator)
    return info.get("eng") if info else None


def _combine(mfr, model, designator):
    """Join manufacturer + model, avoiding a duplicated leading word."""
    if mfr and model and model.lower().startswith(mfr.split()[0].lower()):
        return model                       # e.g. "Gulfstream G650" (don't prefix "Gulfstream")
    parts = [p for p in (mfr, model) if p]
    return " ".join(parts) if parts else (designator or None)


def trim_model(model):
    """Strip airline customer/variant codes that make labels noisy.

    Conservative: cut at a token with a '/' (Boeing winglet codes like '7H4/W',
    '990ER/W') and drop a trailing random code (digits + 4+ letters, e.g.
    '253NXSL'). Real variants ('300ER', '200LR', 'MAX 8', 'G650') are kept.
    """
    if not model:
        return model
    out = []
    for tok in model.split():
        if "/" in tok:
            break
        out.append(tok)
    if len(out) > 1 and re.fullmatch(r"\d{2,4}[A-Za-z]{4,}", out[-1]):
        out.pop()                          # random code, e.g. "253NXSL"
    if len(out) > 2 and out[-1].isdigit():
        out.pop()                          # trailing customer number, e.g. "CRJ 700 701"
    return " ".join(out) if out else model


def friendly_name(designator, adsbdb_model=None):
    """Full human-readable name for detail views, e.g. 'Cessna 172S'."""
    info = lookup(designator)
    mfr = info.get("mfr") if info else None
    model = (adsbdb_model or "").strip() or (info.get("model") if info else None)
    return _combine(mfr, model, designator)


def friendly_short(designator, adsbdb_model=None):
    """Trimmed name for map labels, e.g. 'Boeing 737NG' (no customer codes)."""
    info = lookup(designator)
    mfr = info.get("mfr") if info else None
    model = trim_model((adsbdb_model or "").strip() or (info.get("model") if info else None))
    return _combine(mfr, model, designator)
