"""
Download one day of adsb.lol globe_history, extract aircraft in the Front
Range GA corridor (bbox 39.45–40.50 N, -105.45–-104.60 W, < 9000 ft MSL),
excluding a 5 nm circle around DEN, and append to tracks_yearly.json.

Usage:
  py import_globe_history.py 2025-04-08 --scan-all
  py import_globe_history.py 2025-04-08 --top 40
  py import_globe_history.py 2025-04-08 --keep         # don't delete archives

One day = ~2.8 GB download. After extraction only the matched aircraft
JSON files (few KB each) remain.
"""
import argparse, datetime, gzip, io, json, math, os, sys, tarfile, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
RANKED = os.path.join(HERE, "aircraft_ranked.json")
CACHE = os.path.join(HERE, "cache")
# Write outside OneDrive to avoid mid-write corruption from cloud sync.
# Per-year files keep each under ~200 MB so JSON parse doesn't choke.
TRACKS_DIR = r"C:\tmp\noise_data"

# ─── Front Range GA corridor capture region ──────────────────────────────────
# Rectangular bbox covering KBDU, KLMO, KEIK, KBJC, KAPA, KGXY + approaches.
# DEN exclusion keeps airline traffic out of the dataset.
KBDU_LAT, KBDU_LON = 40.0394, -105.2258

REGION_LAT_MIN = 39.45
REGION_LAT_MAX = 40.50
REGION_LON_MIN = -105.45
REGION_LON_MAX = -104.60
ALT_MAX_FT = 9000

# DEN exclusion — 5 nm circle around the airport reference point.
DEN_LAT, DEN_LON = 39.8617, -104.6731
DEN_EXCLUSION_NM = 5

# Legacy alias kept so the scan-all bbox builder still works.
RADIUS_NM = 15

def nm_between(lat1, lon1, lat2, lon2):
    dlat = (lat1 - lat2) * 60.0
    dlon = (lon1 - lon2) * 60.0 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dlat, dlon)

def nm_from_kbdu(lat, lon):
    return nm_between(lat, lon, KBDU_LAT, KBDU_LON)

def in_capture_region(lat, lon):
    """Return True if (lat, lon) is inside the Front Range corridor bbox
    and outside the DEN exclusion circle."""
    if lat < REGION_LAT_MIN or lat > REGION_LAT_MAX:
        return False
    if lon < REGION_LON_MIN or lon > REGION_LON_MAX:
        return False
    if nm_between(lat, lon, DEN_LAT, DEN_LON) <= DEN_EXCLUSION_NM:
        return False
    return True

def url_exists(url):
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "noise-kbdu/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=30):
            return True
    except urllib.error.HTTPError:
        return False

def download(url, dest):
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        # Verify the cache against the remote's Content-Length before reusing.
        # Orphaned partial downloads from a crashed prior run would otherwise
        # get blindly reused and later fail during tar streaming.
        local_size = os.path.getsize(dest)
        try:
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "noise-kbdu/0.1"})
            with urllib.request.urlopen(req, timeout=30) as r:
                remote_size = int(r.headers.get("Content-Length") or 0)
        except Exception:
            remote_size = 0
        if remote_size and local_size != remote_size:
            print(f"  [stale] {os.path.basename(dest)} local={local_size:,} remote={remote_size:,}, redownloading")
            try:
                os.remove(dest)
            except OSError:
                pass
        else:
            print(f"  [cached] {os.path.basename(dest)} ({local_size:,} bytes)")
            return
    print(f"  downloading {os.path.basename(dest)} ...")
    req = urllib.request.Request(url, headers={"User-Agent": "noise-kbdu/0.1"})
    with urllib.request.urlopen(req, timeout=900) as r, open(dest, "wb") as fh:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        chunk = 1024 * 1024
        last_pct = -1
        while True:
            buf = r.read(chunk)
            if not buf:
                break
            fh.write(buf)
            got += len(buf)
            if total:
                pct = int(got * 100 / total)
                if pct != last_pct and pct % 5 == 0:
                    print(f"    {pct}% ({got/1e6:.0f} MB / {total/1e6:.0f} MB)")
                    last_pct = pct
    print(f"    done — {os.path.getsize(dest):,} bytes")

def resolve_asset_urls(tag, year):
    """Older releases publish a single .tar; newer ones split into .tar.aa/.tar.ab.
    Return the list of URLs to download, in concatenation order."""
    base = f"https://github.com/adsblol/globe_history_{year}/releases/download/{tag}/{tag}"
    single = f"{base}.tar"
    split = [f"{base}.tar.aa", f"{base}.tar.ab"]
    if url_exists(single):
        return [single]
    return split

def concatenated(parts):
    """Yield bytes from multiple files as if concatenated, streaming."""
    for p in parts:
        with open(p, "rb") as fh:
            while True:
                buf = fh.read(4 * 1024 * 1024)
                if not buf:
                    break
                yield buf

class StreamReader(io.RawIOBase):
    """Wrap a byte-chunk generator as a readable non-seekable stream."""
    def __init__(self, gen):
        self._gen = gen
        self._buf = b""
    def readable(self):
        return True
    def readinto(self, b):
        while len(self._buf) < len(b):
            try:
                self._buf += next(self._gen)
            except StopIteration:
                break
        n = min(len(b), len(self._buf))
        b[:n] = self._buf[:n]
        self._buf = self._buf[n:]
        return n

def extract_targets(parts, targets):
    """Stream the concatenated tar, yield (hex, decoded_json) for matching aircraft."""
    wanted = {f"traces/{h[-2:]}/trace_full_{h}.json" for h in targets}
    print(f"  streaming tar, looking for {len(wanted)} aircraft files...")
    found = {}
    reader = StreamReader(concatenated(parts))
    with tarfile.open(fileobj=reader, mode="r|") as tar:
        for ti, member in enumerate(tar):
            if ti and ti % 50000 == 0:
                print(f"    scanned {ti} entries, matched {len(found)}")
            name = member.name.lstrip("./")
            if name in wanted:
                fh = tar.extractfile(member)
                if fh is None:
                    continue
                raw = fh.read()
                if len(raw) >= 2 and raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
                try:
                    data = json.loads(raw)
                except Exception as e:
                    print(f"    parse fail {name}: {e}")
                    continue
                hex_id = name.split("_")[-1].rsplit(".json", 1)[0]
                found[hex_id] = data
                print(f"    [ok] {hex_id} ({len(raw)} bytes)")
                if len(found) == len(wanted):
                    break
    return found

def scan_all_by_location(parts, bbox):
    """Scan EVERY trace_full file in the tar; keep aircraft whose trace has
    any point inside the KBDU envelope (bbox + altitude cap).

    bbox: (lat_min, lat_max, lon_min, lon_max) — a fast pre-filter before the
    precise nm_from_kbdu distance check that the caller applies.
    """
    lat_min, lat_max, lon_min, lon_max = bbox
    print(f"  full scan — keeping aircraft with any point in the bbox "
          f"({lat_min:.2f}..{lat_max:.2f}, {lon_min:.2f}..{lon_max:.2f})")
    found = {}
    reader = StreamReader(concatenated(parts))
    scanned = 0
    with tarfile.open(fileobj=reader, mode="r|") as tar:
        for member in tar:
            name = member.name.lstrip("./")
            if not name.startswith("traces/") or "trace_full_" not in name:
                continue
            scanned += 1
            if scanned % 25000 == 0:
                print(f"    scanned {scanned} traces, matched {len(found)}")
            fh = tar.extractfile(member)
            if fh is None:
                continue
            raw = fh.read()
            if len(raw) >= 2 and raw[:2] == b"\x1f\x8b":
                try:
                    raw = gzip.decompress(raw)
                except Exception:
                    continue
            try:
                data = json.loads(raw)
            except Exception:
                continue
            trace = data.get("trace") or []
            hit = False
            for row in trace:
                if len(row) < 4:
                    continue
                _t, lat, lon, alt, *rest = row
                if lat is None or lon is None or not isinstance(alt, (int, float)):
                    continue
                if int(alt) <= 0 or int(alt) >= ALT_MAX_FT:
                    continue
                if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
                    hit = True
                    break
            if not hit:
                continue
            hex_id = name.split("_")[-1].rsplit(".json", 1)[0]
            found[hex_id] = data
    print(f"  done — scanned {scanned} traces, matched {len(found)}")
    return found

def trace_to_points(data, year):
    """Convert adsb.lol trace_full JSON to our [lat,lon,alt,t_sec] point list,
    filtered to the KBDU envelope. t_sec is seconds since the trace's own
    t0 epoch (data["timestamp"]) — a monotonic integer that lets downstream
    code compute true Δt between consecutive samples."""
    trace = data.get("trace") or []
    pts = []
    for row in trace:
        if len(row) < 4:
            continue
        t_sec, lat, lon, alt, *rest = row
        if lat is None or lon is None:
            continue
        if alt == "ground":
            continue
        if not isinstance(alt, (int, float)):
            continue
        alt_ft = int(alt)
        if alt_ft <= 0 or alt_ft >= ALT_MAX_FT:
            continue
        if not in_capture_region(lat, lon):
            continue
        # t_sec is typically an int or float offset from the file's
        # "timestamp" field; round to int for compactness.
        pts.append([round(lat, 5), round(lon, 5), alt_ft, int(t_sec)])
    return pts

def extract_meta(data):
    """Pull the type / registration / description / operator fields from the trace file."""
    return {
        "type": (data.get("t") or "").strip(),
        "reg": (data.get("r") or "").strip(),
        "desc": (data.get("desc") or "").strip(),
        "ownOp": (data.get("ownOp") or "").strip(),
        "t0": data.get("timestamp"),  # epoch seconds — wallclock of the trace's time-zero
    }

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("date", help="YYYY-MM-DD")
    ap.add_argument("--top", type=int, default=40,
                    help="(legacy) when --scan-all is NOT set, pull only the top N aircraft")
    ap.add_argument("--scan-all", action="store_true",
                    help="scan every aircraft in the archive, keep anything in the KBDU envelope")
    ap.add_argument("--keep", action="store_true", help="keep .tar.aa/.tar.ab after extraction")
    args = ap.parse_args()

    date = args.date
    year = date[:4]
    tag = f"v{date.replace('-', '.')}-planes-readsb-prod-0"

    os.makedirs(CACHE, exist_ok=True)
    urls = resolve_asset_urls(tag, year)
    parts = [os.path.join(CACHE, os.path.basename(u)) for u in urls]
    print(f"  asset layout: {'single .tar' if len(urls) == 1 else 'split .tar.aa/.tar.ab'}")

    # Load the ranked list only if we need it (for label fallback in scan-all,
    # or as a hard target list in legacy mode).
    ranked = []
    if os.path.exists(RANKED):
        with open(RANKED) as fh:
            ranked = json.load(fh)

    for u, p in zip(urls, parts):
        download(u, p)

    if args.scan_all:
        # Bbox matches the Front Range corridor region with a small pad
        # so the scan_all first-pass filter doesn't clip edges.
        bbox = (
            REGION_LAT_MIN - 0.02,
            REGION_LAT_MAX + 0.02,
            REGION_LON_MIN - 0.02,
            REGION_LON_MAX + 0.02,
        )
        found = scan_all_by_location(parts, bbox)
    else:
        targets = [r["hex"] for r in ranked[: args.top]]
        print(f"targets: {len(targets)} aircraft (top {args.top})")
        found = extract_targets(parts, targets)
        print(f"\nmatched {len(found)} / {len(targets)} target aircraft")

    # Build tracks in our standard shape
    new_tracks = []
    reg_lookup = {r["hex"]: (r["calls"][0] if r.get("calls") else r["hex"]) for r in ranked}
    for hex_id, data in found.items():
        pts = trace_to_points(data, year)
        if len(pts) < 3:
            print(f"  {hex_id}: only {len(pts)} points in KBDU envelope — skip")
            continue
        meta = extract_meta(data)
        call = meta["reg"] or reg_lookup.get(hex_id, hex_id)
        new_tracks.append({
            "call": call,
            "type": meta["type"],
            "desc": meta["desc"],
            "ownOp": meta["ownOp"],
            "src": f"globe/{date}/{hex_id}",
            "points": pts,
            "t0": meta.get("t0"),       # epoch seconds — trace time-zero
            "year": year,
            "years_back": datetime.date.today().year - int(year),
        })
        print(f"  {hex_id} ({meta['type'] or '?'}): {len(pts)} points kept")

    # Idempotent merge into the YEAR-SPECIFIC file (e.g. tracks_2024.json).
    # Each file stays small enough for reliable JSON parse + atomic write.
    os.makedirs(TRACKS_DIR, exist_ok=True)
    year_file = os.path.join(TRACKS_DIR, f"tracks_{year}.json")
    existing = {"tracks": []}
    if os.path.exists(year_file):
        with open(year_file) as fh:
            existing = json.load(fh)
    new_srcs = {t["src"] for t in new_tracks}
    kept = [t for t in existing.get("tracks", []) if t.get("src") not in new_srcs]
    merged = kept + new_tracks

    payload = {
        "year": year,
        "center": [KBDU_LAT, KBDU_LON],
        "region": {
            "lat_min": REGION_LAT_MIN, "lat_max": REGION_LAT_MAX,
            "lon_min": REGION_LON_MIN, "lon_max": REGION_LON_MAX,
            "den_exclusion_nm": DEN_EXCLUSION_NM,
        },
        "alt_max_ft": ALT_MAX_FT,
        "tracks": merged,
    }
    tmp_path = year_file + ".tmp"
    with open(tmp_path, "w") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    replaced = False
    for attempt in range(6):
        try:
            os.replace(tmp_path, year_file)
            replaced = True
            break
        except PermissionError:
            time.sleep(0.5 * (attempt + 1))
    if not replaced:
        with open(year_file, "w") as fh:
            json.dump(payload, fh, separators=(",", ":"))
        try: os.remove(tmp_path)
        except FileNotFoundError: pass
    print(f"\nwrote {len(new_tracks)} new tracks (total {len(merged)}) -> {year_file}")

    if not args.keep:
        for p in parts:
            for attempt in range(4):
                try:
                    os.remove(p)
                    print(f"  removed {os.path.basename(p)}")
                    break
                except FileNotFoundError:
                    break
                except PermissionError:
                    if attempt < 3:
                        time.sleep(0.5 * (attempt + 1))
                    else:
                        print(f"  could not remove {os.path.basename(p)} (still locked)")

if __name__ == "__main__":
    main()
