"""
Extract KBDU-area aircraft tracks from Den_multi pickles into tracks.json
for the noise visualization web app.

Filters:
  - within RADIUS_NM of KBDU
  - altitude below ALT_MAX_FT (MSL)
  - at least MIN_POINTS positions after filtering

Output: noise/web/public/tracks.json
"""
import pickle, glob, json, math, os, sys

KBDU_LAT, KBDU_LON = 40.0394, -105.2258
RADIUS_NM = 10.0
ALT_MAX_FT = 7500
MIN_POINTS = 3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "public", "tracks.json")

sys.path.insert(0, ROOT)  # so pickle can find aircraft / location / shapely-using modules

def nm_from_kbdu(lat, lon):
    dlat = (lat - KBDU_LAT) * 60.0
    dlon = (lon - KBDU_LON) * 60.0 * math.cos(math.radians((lat + KBDU_LAT) / 2))
    return math.hypot(dlat, dlon)

def main():
    pickles = sorted(
        glob.glob(os.path.join(ROOT, "Den_multi*.pkl"))
        + glob.glob(os.path.join(ROOT, "pickles", "Den_multi*.pkl"))
    )
    print(f"Scanning {len(pickles)} pickles...")

    tracks = []
    for pkl in pickles:
        try:
            with open(pkl, "rb") as fh:
                ac_list = pickle.load(fh)
        except Exception as e:
            print(f"  skip {os.path.basename(pkl)}: {e}")
            continue

        file_tag = os.path.basename(pkl).replace(".pkl", "")
        kept_here = 0
        for ac in ac_list:
            pts = []
            for loc in ac.locations:
                if loc.lat == 0 and loc.lon == 0:
                    continue
                if loc.alt is None or loc.alt <= 0 or loc.alt >= ALT_MAX_FT:
                    continue
                if nm_from_kbdu(loc.lat, loc.lon) > RADIUS_NM:
                    continue
                pts.append([round(loc.lat, 5), round(loc.lon, 5), int(loc.alt)])
            if len(pts) < MIN_POINTS:
                continue
            tracks.append({
                "call": (ac.call or "").strip() or "?",
                "type": getattr(ac, "type", "") or "",
                "src": file_tag,
                "points": pts,
            })
            kept_here += 1
        print(f"  {file_tag}: kept {kept_here} tracks")

    # Deduplicate identical tracks across pickles (same call + same first/last point)
    seen, unique = set(), []
    for t in tracks:
        key = (t["call"], tuple(t["points"][0]), tuple(t["points"][-1]), len(t["points"]))
        if key in seen:
            continue
        seen.add(key)
        unique.append(t)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as fh:
        json.dump({
            "center": [KBDU_LAT, KBDU_LON],
            "radius_nm": RADIUS_NM,
            "alt_max_ft": ALT_MAX_FT,
            "tracks": unique,
        }, fh, separators=(",", ":"))

    total_pts = sum(len(t["points"]) for t in unique)
    print(f"\nWrote {len(unique)} unique tracks ({total_pts} points) -> {OUT}")

if __name__ == "__main__":
    main()
