"""
Fetch live ADS-B aircraft around KBDU from adsb.lol (free, no API key).

Usage:
  py fetch_adsb.py                 # one snapshot, save + summarize
  py fetch_adsb.py --loop 30       # poll every 30s until Ctrl+C
  py fetch_adsb.py --radius 15     # override radius in NM
"""
import argparse, datetime, json, math, os, sys, time, urllib.request

KBDU_LAT, KBDU_LON = 40.0394, -105.2258
KBDU_ELEV_FT = 5288
DEFAULT_RADIUS_NM = 10
ALT_MAX_FT = 7500

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

def fetch(lat, lon, radius_nm, timeout=15):
    url = f"https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{radius_nm}"
    req = urllib.request.Request(url, headers={"User-Agent": "noise-kbdu/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))

def nm_from_kbdu(lat, lon):
    dlat = (lat - KBDU_LAT) * 60.0
    dlon = (lon - KBDU_LON) * 60.0 * math.cos(math.radians((lat + KBDU_LAT) / 2))
    return math.hypot(dlat, dlon)

def alt_feet(ac):
    a = ac.get("alt_baro")
    if a == "ground":
        return 0
    if isinstance(a, (int, float)):
        return int(a)
    return None

def summarize(payload, radius_nm):
    ac_list = payload.get("ac") or []
    rows, low = [], 0
    for ac in ac_list:
        lat, lon = ac.get("lat"), ac.get("lon")
        if lat is None or lon is None:
            continue
        dist = nm_from_kbdu(lat, lon)
        alt = alt_feet(ac)
        if alt is not None and alt < ALT_MAX_FT:
            low += 1
        rows.append({
            "hex": ac.get("hex"),
            "call": (ac.get("flight") or "").strip(),
            "type": ac.get("t") or "",
            "reg": ac.get("r") or "",
            "alt": alt,
            "gs": ac.get("gs"),
            "trk": ac.get("track"),
            "lat": lat,
            "lon": lon,
            "dist_nm": round(dist, 2),
        })
    rows.sort(key=lambda r: r["dist_nm"])
    print(f"  {len(rows)} aircraft within {radius_nm} nm · {low} below {ALT_MAX_FT} ft")
    for r in rows[:12]:
        call = (r["call"] or r["reg"] or r["hex"] or "?")[:9]
        alt = f"{r['alt']:>5}" if r["alt"] is not None else "  ---"
        gs = f"{r['gs']:>4.0f}" if isinstance(r["gs"], (int, float)) else "  --"
        print(f"    {call:<9} {r['type']:<5} alt {alt} ft  gs {gs} kt  dst {r['dist_nm']:>5.2f} nm")
    if len(rows) > 12:
        print(f"    ... {len(rows) - 12} more")
    return rows

def save(payload, rows):
    os.makedirs(DATA_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(DATA_DIR, f"kbdu_{ts}.json")
    with open(path, "w") as fh:
        json.dump({
            "fetched_at": ts,
            "center": [KBDU_LAT, KBDU_LON],
            "raw_count": len(payload.get("ac") or []),
            "rows": rows,
        }, fh, separators=(",", ":"))
    return path

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--radius", type=int, default=DEFAULT_RADIUS_NM)
    ap.add_argument("--loop", type=int, default=0, help="poll interval seconds (0 = one shot)")
    args = ap.parse_args()

    print(f"adsb.lol  KBDU {KBDU_LAT},{KBDU_LON}  radius {args.radius} nm")
    while True:
        t0 = time.time()
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        try:
            payload = fetch(KBDU_LAT, KBDU_LON, args.radius)
            print(f"[{ts}] fetched in {time.time()-t0:.2f}s")
            rows = summarize(payload, args.radius)
            path = save(payload, rows)
            print(f"  saved -> {os.path.relpath(path)}")
        except Exception as e:
            print(f"[{ts}] error: {e}")
        if args.loop <= 0:
            break
        time.sleep(args.loop)

if __name__ == "__main__":
    main()
