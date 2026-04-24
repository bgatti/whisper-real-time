"""
Test /states/all with a KBDU bounding box. One snapshot per call.

Uses the same OAuth2 credentials + fast-fail 429 handler from fetch_opensky.py.
"""
import json, sys, time, datetime
from fetch_opensky import get, RateLimited, _last_remaining

# KBDU ~15 nm bbox (roomy — KDEN sits outside so we don't get every airliner)
LAMIN, LAMAX = 39.89, 40.19
LOMIN, LOMAX = -105.55, -104.90
KBDU_LAT, KBDU_LON = 40.0394, -105.2258

def snap(ts):
    url = (
        f"https://opensky-network.org/api/states/all"
        f"?time={int(ts)}&lamin={LAMIN}&lamax={LAMAX}&lomin={LOMIN}&lomax={LOMAX}"
    )
    return get(url)

def main():
    # Three test calls: right now, 1 hour ago, yesterday same time.
    now = int(time.time())
    tests = [
        ("now", now),
        ("1h ago", now - 3600),
        ("24h ago", now - 86400),
    ]
    for label, ts in tests:
        when = datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M UTC")
        print(f"\n=== {label} ({when}) ===")
        try:
            data = snap(ts)
        except RateLimited as e:
            print(f"  RATE LIMITED: {e}")
            break
        except Exception as e:
            print(f"  ERROR: {e}")
            continue
        states = data.get("states") or []
        print(f"  {len(states)} states returned")
        for s in states[:8]:
            icao, call, _, _, _, lon, lat, baro, on_ground, gs, trk, vs, _, geo_alt, *_ = s
            alt_ft = int((baro or 0) * 3.28084) if baro else None
            vs_fpm = int((vs or 0) * 196.85) if vs else 0
            print(f"    {icao} {(call or '').strip():<8} alt {alt_ft} ft  vs {vs_fpm:+5} fpm  gs {gs}")
        if _last_remaining is not None:
            print(f"  credits remaining: {_last_remaining}")
        time.sleep(2)

if __name__ == "__main__":
    main()
