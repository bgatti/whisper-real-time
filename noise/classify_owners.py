"""Heuristic owner classification for every tail seen in the tracks files.

Writes noise/owners.json with tails grouped into school / club / corp /
private buckets. Classification is a guess based on ICAO type + sighting
count (we don't have live FAA registry access):

  school   Tail already listed in flight_schools_fleets.json.
  club     Not in schools file, but ICAO type is a common trainer AND the
           tail shows up >= 5 times — fits a flying club or unlisted
           school/FBO rental pattern.
  corp     Jets, turboprops, twins, or non-training helicopters. These
           almost never belong to a private individual.
  private  Everything else — low-count sightings, singles, warbirds, etc.
"""

from __future__ import annotations

import json
from pathlib import Path

# --- type buckets ---
TRAINER_TYPES = {
    "C140", "C150", "C152", "C162", "C172", "C72R", "C177",
    "DA40", "DV20", "SR20", "SP20",
    "P28A", "P28B", "P28R", "P32R",
    "BE76", "PA44",            # light twins used for multi training
    "AA5", "CH7A", "CH7B", "CRUZ", "RV12",
    "PA11", "PA12", "PA16", "PA18",
}
TRAINER_HELIS = {"R44", "H500", "B06"}

# Glider tow planes. Private ownership is rare — almost always a soaring
# club or commercial glider operation. Treated as club when not already
# in a known school/club listing.
TOW_TYPES = {"PA25", "PA18", "HUSK"}

HEAVY_CORP_TYPES = {
    # jets
    "B38M", "B752", "B737", "B739",
    "C25A", "C25B", "C25C", "C25M", "C510", "C525", "C55B",
    "C560", "C56X", "C650", "C680", "C68A", "C700", "C750",
    "CL30", "CL35", "CL60", "GLEX", "GLF4", "GLF5", "GLF6",
    "E135", "E145", "E55P", "F2TH", "F900", "FA50", "FA7X",
    "G200", "G280", "H25B", "HDJT", "LJ31", "LJ35", "LJ45", "LJ60", "LJ70",
    # turboprops
    "BE30", "B350", "PC12", "M600", "TBM7", "TBM8", "TBM9",
    "PA46", "P46T", "PAY1", "AC90", "C208", "P06T",
    # twins (piston and turboprop)
    "BE55", "BE58", "C310", "C340", "C402", "C404", "C414", "C421",
    "C425", "C441", "PA34", "P337",
    # non-training helicopters
    "B407", "AS50", "AS32", "EC20", "EC25", "EC30", "EC35", "EC45",
    "UH1", "SW3",
}

PRIVATE_HIGHPERF = {
    "SR22", "S22T", "BE35", "BE36", "M20P", "M20T", "P28T",
    "COL3", "COL4",
}


def classify(tail, types, count, in_schools):
    """Returns (category, inferred_school_name). Inferred name is None
    unless we're guessing the owner from type alone."""
    if in_schools:
        return "school", None
    if types & HEAVY_CORP_TYPES:
        return "corp", None
    # Glider-tow-capable types around KBDU almost always belong to the
    # local soaring operations. We attribute every unmatched tow plane
    # to Mile High Gliding — the dominant operator — so they show up
    # under a named school rather than as anonymous clubs.
    if types & TOW_TYPES and count >= 2:
        return "school", "Mile High Gliding (inferred)"
    if types & (TRAINER_TYPES | TRAINER_HELIS) and count >= 5:
        return "club", None
    return "private", None


def main() -> int:
    pub = Path("noise/web/public")
    schools = json.loads(
        Path("noise/flight_schools_fleets.json").read_text()
    )
    school_tails = {}  # tail -> school name
    for s in schools.get("schools", []):
        for a in s.get("aircraft", []):
            if a.get("tail"):
                school_tails[a["tail"]] = s.get("name", "")

    seen = {}  # tail -> {types: set, count, last_type}
    for fn in pub.iterdir():
        if not fn.name.startswith("tracks") or fn.suffix != ".json":
            continue
        try:
            d = json.loads(fn.read_text())
        except Exception:
            continue
        for t in d.get("tracks", []):
            call = (t.get("call") or "").strip()
            if not call:
                continue
            e = seen.setdefault(call, {"types": set(), "count": 0})
            if t.get("type"):
                e["types"].add(t["type"])
            e["count"] += 1

    buckets = {"school": [], "club": [], "corp": [], "private": []}
    for tail, info in sorted(seen.items(), key=lambda kv: -kv[1]["count"]):
        in_schools = tail in school_tails
        cat, inferred = classify(tail, info["types"], info["count"], in_schools)
        school_name = school_tails.get(tail) if in_schools else inferred
        buckets[cat].append({
            "tail": tail,
            "types": sorted(info["types"]),
            "count": info["count"],
            "school": school_name,
            "inferred": inferred is not None,
        })

    totals = {k: len(v) for k, v in buckets.items()}
    out = {
        "generated_from": "track files under noise/web/public",
        "totals": totals,
        "buckets": buckets,
    }
    out_path = pub / "owners.json"
    out_path.write_text(json.dumps(out, indent=2))
    print(f"wrote {out_path}  total={sum(totals.values())}")
    for k, v in totals.items():
        print(f"  {k:8s}: {v}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
