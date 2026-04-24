"""Classify special-use aircraft from tracks_yearly.json.

Writes noise/data/special_use_aircraft.json with tail, use category,
and description for medivac, firefighting, law enforcement, military,
science, survey, patrol, and government aircraft.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

TRACKS_PATH = os.path.join("C:", os.sep, "tmp", "noise_data", "tracks_yearly.json")
OUT_PATH = Path("noise/data/special_use_aircraft.json")

# ---- hospital helipad locations (lat, lon, name) ----
HOSPITALS = [
    (39.7392, -104.9384, "Denver Health"),
    (39.7478, -104.9730, "St Anthony Central"),
    (40.0150, -105.2483, "Boulder Community Foothills"),
    (40.0207, -105.2116, "Boulder Community Broadway"),
    (39.6771, -104.9389, "Swedish Medical Englewood"),
    (39.5814, -105.0864, "Littleton Adventist"),
    (39.6519, -105.1509, "St Anthony Lakewood"),
    (39.8580, -104.6947, "UCHealth Aurora"),
    (39.7446, -104.8416, "Anschutz/UCH Aurora"),
    (40.4049, -105.0753, "UCHealth Loveland"),
    (40.5548, -105.0641, "Poudre Valley Ft Collins"),
    (39.5986, -104.8361, "Sky Ridge Medical"),
    (39.9169, -105.0081, "North Suburban Thornton"),
    (39.8747, -104.9811, "St Anthony North Westminster"),
    (39.6395, -104.7836, "Parker Adventist"),
    (40.1697, -105.1019, "Longmont United"),
    (39.7620, -104.7734, "Childrens Hospital CO"),
    (39.6200, -104.8950, "Centennial Medical Plaza"),
    (39.5310, -104.7860, "Castle Rock Adventist"),
]

HELI_TYPES = {
    "AS50", "AS32", "B06", "B407", "EC20", "EC25", "EC30", "EC35", "EC45",
    "H500", "R44", "R66", "S76", "A109", "A139", "BK17", "S70", "UH1", "SW3",
    "H60", "B412", "B429", "B505", "MD52", "MD50", "ASTR", "EC55", "EC75",
}

RADIUS_NM = 0.5  # ~3000 ft from hospital


def haversine_nm(lat1, lon1, lat2, lon2):
    R = 3440.065
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1))
         * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def near_hospital(lat, lon):
    for hlat, hlon, hname in HOSPITALS:
        if haversine_nm(lat, lon, hlat, hlon) < RADIUS_NM:
            return hname
    return None


# ---- owner keyword groups ----
MEDIVAC_OWNERS = [
    "AIR METHODS", "MED-TRANS", "MED TRANS", "REACH AIR MEDICAL",
    "LIFE FLIGHT", "LIFEFLIGHT", "CARE FLIGHT", "CAREFLIGHT",
    "ELITE MEDICAL AIR", "LYNX MEDICAL", "CLASSIC MEDICAL",
    "PHI AIR MEDICAL", "OMNIFLIGHT", "AERO CARE",
]
LAW_OWNERS = ["STATE PATROL", "POLICE", "SHERIFF", "HIGHWAY PATROL"]
FIRE_OWNERS = [
    "DIVISION OF FIRE", "FIRE AND AVIATION", "CO FIRE AVIATION",
    "CAL FIRE", "DEPT OF FORESTRY", "FIRE PROTECTION",
]
GOVT_OWNERS = [
    "STATE OF COLORADO", "US DEPARTMENT", "UNITED STATES DEPARTMENT",
    "DEPARTMENT OF TRANSPORTATION", "DEPARTMENT OF NATURAL RESOURCES",
    "DEPARTMENT OF ENERGY", "DEPARTMENT OF PUBLIC SAFETY",
]
MILITARY_OWNERS = [
    "UNITED STATES AIR FORCE", "AIR FORCE ACADEMY",
    "AERONAUTICAL SYSTEMS GROUP", "NATIONAL GUARD", "U.S. AIR FORCE",
]
SCIENCE_OWNERS = [
    "NOAA", "NATIONAL SCIENCE FOUNDATION", "UNIVERSITY RESEARCH",
    "GENERAL ATOMICS AERONAUTICAL", "GENERAL DYNAMICS MISSION",
]
SURVEY_OWNERS = [
    "AERIAL SURVEY", "KEYSTONE AERIAL", "SURVEYING AND MAPPING",
    "COSTAR FIELD RESEARCH",
]
PATROL_OWNERS = [
    "AERIAL PATROL", "AIR PATROL", "AMERICAN PATROLS",
    "EAGLE SKY PATROL", "KCSI AERIAL", "BARR AIR PATROL",
]
SAR_OWNERS = ["CIVIL AIR PATROL"]
HELICOPTER_SVC = [
    "HELICOPTERS INC", "HELICOPTER LLC", "HELICOPTER SERVICES",
    "HELICOPTER EXPRESS", "RAMPART HELICOPTER", "HAWKEYE HELICOPTER",
    "SPITZER HELICOPTER", "GULF COAST HELICOPTER", "BLACK HILLS HELICOPTER",
    "TIMBERLINE HELICOPTER", "MOUNTAIN WEST HELICOPTER",
    "TOUCHSTONE HELICOPTER", "CONSTRUCTION HELICOPTER",
    "BEACH HELICOPTER", "FALCON HELICOPTER", "DRB HELICOPTER",
    "COPTER LEASE",
]
MEDICAL_FACILITY = [
    "HOSPITAL", "HEALTH CENTER", "HEALTH SERVICES", "HEALTHCARE",
    "MEDICAL CENTER", "NOVANT HEALTH", "SANFORD HEALTH", "SCL HEALTH",
    "CENTURA", "IHC HEALTH", "LOGAN HEALTH",
]


def classify_special(tail, info):
    owners_upper = " | ".join(info["owners"]).upper()
    is_heli = bool(info["types"] & HELI_TYPES)
    has_hosp = len(info["hosp_endpoints"]) > 0

    def match(keywords):
        return any(kw in owners_upper for kw in keywords)

    # Medivac: named EMS operator, OR helicopter with hospital endpoints
    if match(MEDIVAC_OWNERS):
        return "medivac", "EMS helicopter operator"
    if is_heli and info["hosp_total"] >= 3:
        return "medivac", "helicopter with repeated hospital landings"
    if match(MEDICAL_FACILITY) and is_heli:
        return "medivac", "hospital-owned helicopter"

    if match(FIRE_OWNERS):
        return "firefighting", "fire agency aircraft"
    if match(LAW_OWNERS):
        return "law_enforcement", "law enforcement agency"
    if match(SAR_OWNERS):
        return "search_rescue", "Civil Air Patrol search & rescue"
    if match(MILITARY_OWNERS):
        return "military", "US military aircraft"
    if match(SCIENCE_OWNERS):
        return "science", "research/science agency"
    if match(SURVEY_OWNERS):
        return "survey", "aerial survey operator"
    if match(PATROL_OWNERS):
        return "patrol", "aerial patrol (pipeline/powerline)"
    if match(GOVT_OWNERS):
        return "government", "state/federal government agency"
    if match(HELICOPTER_SVC):
        return "helicopter_ops", "commercial helicopter operator (utility/charter)"

    # Anonymous hex tails at hospitals are very likely medivac
    if tail.startswith("~") and info["hosp_total"] >= 3:
        return "medivac", "unregistered hex near hospitals (probable EMS)"

    # Heli at hospital but low count
    if is_heli and has_hosp:
        return "medivac_possible", "helicopter seen near hospital"

    return None, None


def main():
    d = json.load(open(TRACKS_PATH, encoding="utf-8"))
    tracks = d.get("tracks", [])

    # Pass 1: aggregate per tail
    tails = {}
    for t in tracks:
        call = (t.get("call") or "").strip()
        if not call:
            continue
        if call not in tails:
            tails[call] = {
                "types": set(), "descs": set(), "owners": set(),
                "count": 0, "hosp_endpoints": set(), "hosp_total": 0,
            }
        e = tails[call]
        if t.get("type"):
            e["types"].add(t["type"])
        if t.get("desc"):
            e["descs"].add(t["desc"])
        if t.get("ownOp"):
            e["owners"].add(t["ownOp"].strip())
        e["count"] += 1

        # Check first/last 3 points for hospital proximity
        pts = t.get("points", [])
        endpoints = pts[:3] + pts[-3:]
        for pt in endpoints:
            h = near_hospital(pt[0], pt[1])
            if h:
                e["hosp_endpoints"].add(h)
                e["hosp_total"] += 1
                break  # one hit per track is enough

    # Pass 2: classify
    results = []
    for tail, info in tails.items():
        use, desc = classify_special(tail, info)
        if use is None:
            continue
        entry = {
            "tail": tail,
            "use": use,
            "description": desc,
            "owner": " / ".join(sorted(info["owners"])) if info["owners"] else "",
            "aircraft_type": ",".join(sorted(info["types"])) if info["types"] else "",
            "aircraft_desc": " / ".join(sorted(info["descs"])) if info["descs"] else "",
            "track_count": info["count"],
        }
        if info["hosp_endpoints"]:
            entry["hospitals"] = sorted(info["hosp_endpoints"])
        results.append(entry)

    results.sort(key=lambda x: (x["use"], -x["track_count"]))

    by_use = {}
    for r in results:
        by_use[r["use"]] = by_use.get(r["use"], 0) + 1

    out = {
        "generated": "2026-04-17",
        "source": "tracks_yearly.json special-use classification",
        "total": len(results),
        "by_use": by_use,
        "aircraft": results,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2))
    print(f"Wrote {OUT_PATH}")
    print(f"Total special-use aircraft: {len(results)}\n")
    for use, cnt in sorted(by_use.items(), key=lambda x: -x[1]):
        print(f"  {use:20s}: {cnt}")


if __name__ == "__main__":
    main()
