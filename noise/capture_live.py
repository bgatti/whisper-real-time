"""
Poll adsb.lol live and accumulate aircraft tracks for the web app.

Produces noise/web/public/tracks_live.json in the same shape as tracks.json,
so the React app can point at either one.

Usage:
  py capture_live.py                  # poll every 20s until Ctrl+C
  py capture_live.py --interval 10    # custom interval
  py capture_live.py --radius 15
"""
import argparse, datetime, json, math, os, signal, sys, time

from fetch_adsb import fetch, alt_feet, KBDU_LAT, KBDU_LON, ALT_MAX_FT

WEB_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "public", "tracks_live.json")
RADIUS_DEFAULT = 10
MIN_POINTS = 2

def nm_from_kbdu(lat, lon):
    dlat = (lat - KBDU_LAT) * 60.0
    dlon = (lon - KBDU_LON) * 60.0 * math.cos(math.radians((lat + KBDU_LAT) / 2))
    return math.hypot(dlat, dlon)

def dedup_last(pts, lat, lon):
    """Avoid adding an identical consecutive position."""
    if pts and pts[-1][0] == round(lat, 5) and pts[-1][1] == round(lon, 5):
        return False
    return True

def write(tracks_by_hex, radius_nm, started_at):
    tracks = []
    for hex_id, rec in tracks_by_hex.items():
        if len(rec["points"]) < MIN_POINTS:
            continue
        tracks.append({
            "call": rec["call"] or rec["reg"] or hex_id,
            "type": rec["type"],
            "src": "live",
            "points": rec["points"],
        })
    os.makedirs(os.path.dirname(WEB_OUT), exist_ok=True)
    with open(WEB_OUT, "w") as fh:
        json.dump({
            "center": [KBDU_LAT, KBDU_LON],
            "radius_nm": radius_nm,
            "alt_max_ft": ALT_MAX_FT,
            "started_at": started_at,
            "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "tracks": tracks,
        }, fh, separators=(",", ":"))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=20)
    ap.add_argument("--radius", type=int, default=RADIUS_DEFAULT)
    args = ap.parse_args()

    tracks_by_hex = {}
    started = datetime.datetime.now().isoformat(timespec="seconds")
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
        print("\n  stopping — final flush")
    signal.signal(signal.SIGINT, stop)

    print(f"capture_live  KBDU radius {args.radius} nm  every {args.interval}s  -> {os.path.relpath(WEB_OUT)}")
    poll = 0
    while not stopping:
        poll += 1
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        try:
            payload = fetch(KBDU_LAT, KBDU_LON, args.radius)
            ac_list = payload.get("ac") or []
            added = 0
            for ac in ac_list:
                lat, lon = ac.get("lat"), ac.get("lon")
                if lat is None or lon is None:
                    continue
                alt = alt_feet(ac)
                if alt is None or alt <= 0 or alt >= ALT_MAX_FT:
                    continue
                if nm_from_kbdu(lat, lon) > args.radius:
                    continue
                hex_id = ac.get("hex")
                if not hex_id:
                    continue
                rec = tracks_by_hex.get(hex_id)
                if rec is None:
                    rec = {
                        "call": (ac.get("flight") or "").strip(),
                        "type": ac.get("t") or "",
                        "reg": ac.get("r") or "",
                        "points": [],
                    }
                    tracks_by_hex[hex_id] = rec
                if dedup_last(rec["points"], lat, lon):
                    rec["points"].append([round(lat, 5), round(lon, 5), int(alt)])
                    added += 1
            write(tracks_by_hex, args.radius, started)
            qualifying = sum(1 for r in tracks_by_hex.values() if len(r["points"]) >= MIN_POINTS)
            print(f"[{ts}] poll {poll}  raw {len(ac_list)}  +{added} pts  tracks {qualifying}/{len(tracks_by_hex)}")
        except Exception as e:
            print(f"[{ts}] error: {e}")
        # Sleep in small chunks so Ctrl+C is responsive
        for _ in range(args.interval):
            if stopping: break
            time.sleep(1)

    write(tracks_by_hex, args.radius, started)
    print(f"done — {len(tracks_by_hex)} aircraft seen, file at {WEB_OUT}")

if __name__ == "__main__":
    main()
