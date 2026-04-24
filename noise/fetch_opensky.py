"""
Fetch historical arrivals/departures at KBDU from OpenSky Network.

Endpoints:
  GET /api/flights/arrival?airport=KBDU&begin=...&end=...
  GET /api/flights/departure?airport=KBDU&begin=...&end=...
  GET /api/tracks/all?icao24=...&time=...

Auth:
  Anonymous works for some endpoints but is heavily rate-limited and
  restricted to short time windows. Set OPENSKY_USER / OPENSKY_PASS env
  vars for Basic auth (register free at opensky-network.org).

Usage:
  py fetch_opensky.py --days 2
  py fetch_opensky.py --days 7 --airport KBDU
"""
import argparse, datetime, json, math, os, sys, time
import urllib.request, urllib.error, urllib.parse

KBDU_LAT, KBDU_LON = 40.0394, -105.2258
ALT_MAX_FT = 7500
RADIUS_NM = 15
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "public", "tracks_historical.json")
META_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "public", "flights_yearly.json")
YEARLY_TRACKS_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "public", "tracks_yearly.json")
CREDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.json")
TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"

_token_cache = {"value": None, "expires_at": 0}

def load_creds():
    if os.path.exists(CREDS_FILE):
        with open(CREDS_FILE) as fh:
            d = json.load(fh)
            return d.get("clientId"), d.get("clientSecret")
    return os.environ.get("OPENSKY_CLIENT_ID"), os.environ.get("OPENSKY_CLIENT_SECRET")

def get_token():
    if _token_cache["value"] and time.time() < _token_cache["expires_at"] - 30:
        return _token_cache["value"]
    cid, secret = load_creds()
    if not cid or not secret:
        return None
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": cid,
        "client_secret": secret,
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "noise-kbdu/0.1"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        tok = json.loads(resp.read().decode("utf-8"))
    _token_cache["value"] = tok["access_token"]
    _token_cache["expires_at"] = time.time() + tok.get("expires_in", 300)
    print(f"  obtained OAuth2 token (expires in {tok.get('expires_in')}s)")
    return _token_cache["value"]

def auth_header():
    tok = get_token()
    return {"Authorization": f"Bearer {tok}"} if tok else {}

_last_remaining = None

class RateLimited(Exception):
    """Server returned 429. Carries the retry-after value if present."""
    def __init__(self, url, retry_after=None):
        self.url = url
        self.retry_after = retry_after
        msg = f"429 from {url}"
        if retry_after is not None:
            msg += f" — retry after {retry_after}s ({retry_after/3600:.1f}h)"
        super().__init__(msg)

def get(url, timeout=30):
    global _last_remaining
    req = urllib.request.Request(url, headers={"User-Agent": "noise-kbdu/0.1", **auth_header()})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            remaining = resp.headers.get("X-Rate-Limit-Remaining")
            if remaining is not None:
                _last_remaining = remaining
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            retry = e.headers.get("x-rate-limit-retry-after-seconds") or e.headers.get("X-Rate-Limit-Retry-After-Seconds")
            try:
                retry = int(retry) if retry is not None else None
            except ValueError:
                retry = None
            raise RateLimited(url, retry) from None
        raise

def credits_remaining():
    return _last_remaining

def nm_from_kbdu(lat, lon):
    dlat = (lat - KBDU_LAT) * 60.0
    dlon = (lon - KBDU_LON) * 60.0 * math.cos(math.radians((lat + KBDU_LAT) / 2))
    return math.hypot(dlat, dlon)

def flights(airport, begin, end, direction):
    assert direction in ("arrival", "departure")
    url = (
        f"https://opensky-network.org/api/flights/{direction}"
        f"?airport={airport}&begin={begin}&end={end}"
    )
    rem = f" [credits {_last_remaining}]" if _last_remaining is not None else ""
    print(f"  GET {direction}s {begin}..{end}{rem}")
    try:
        return get(url) or []
    except RateLimited:
        raise  # fail fast — caller prints the retry-after window
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:200]
        print(f"    HTTP {e.code}: {body}")
        return []
    except Exception as e:
        print(f"    error: {e}")
        return []

def track_for(icao24, time_s):
    url = f"https://opensky-network.org/api/tracks/all?icao24={icao24}&time={time_s}"
    try:
        return get(url)
    except RateLimited:
        raise  # caller aborts the whole run
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        print(f"    track {icao24}: HTTP {e.code}")
        return None
    except Exception as e:
        print(f"    track {icao24}: {e}")
        return None

def run_years_tracks(args):
    if not os.path.exists(META_OUT):
        print(f"error: {META_OUT} not found — run --years first"); sys.exit(1)
    with open(META_OUT) as fh:
        meta = json.load(fh)

    cid, _ = load_creds()
    if cid:
        print(f"using OAuth2 client {cid}")
    else:
        print("error: need credentials"); sys.exit(1)

    out_years = []
    for y in meta["years"]:
        yback = y["years_back"]
        flights_list = y["flights"]
        # Prefer flights with lastSeen timestamps (needed for /tracks/all).
        flights_list = [f for f in flights_list if f.get("icao24") and f.get("lastSeen")]
        # Dedup by icao24 keeping the first occurrence — one track per aircraft/year.
        seen = set()
        picks = []
        for f in flights_list:
            if f["icao24"] in seen:
                continue
            seen.add(f["icao24"])
            picks.append(f)
            if len(picks) >= args.years_tracks:
                break

        print(f"\n=== year-{yback} ({y['window_begin']}..{y['window_end']}) — attempting {len(picks)} tracks ===")
        got = []
        for i, f in enumerate(picks):
            icao24 = f["icao24"]
            t = f["lastSeen"]
            rem = f" [credits {_last_remaining}]" if _last_remaining else ""
            print(f"  [{i+1}/{len(picks)}] {icao24} {f['call']:<9}{rem}")
            tr = track_for_retry(icao24, t)
            time.sleep(args.track_delay)
            if not tr or not tr.get("path"):
                continue
            pts = []
            for row in tr["path"]:
                _ts, lat, lon, alt_m, _trk, on_ground = row
                if lat is None or lon is None or on_ground or alt_m is None:
                    continue
                alt_ft = int(alt_m * 3.28084)
                if alt_ft <= 0 or alt_ft >= ALT_MAX_FT:
                    continue
                if nm_from_kbdu(lat, lon) > RADIUS_NM:
                    continue
                pts.append([round(lat, 5), round(lon, 5), alt_ft])
            if len(pts) >= 3:
                got.append({
                    "call": f["call"] or icao24,
                    "type": "",
                    "src": f"opensky/{icao24}",
                    "points": pts,
                    "year": y["window_end"][:4],
                    "years_back": yback,
                })
        print(f"  year-{yback}: got {len(got)} tracks")
        out_years.append({"years_back": yback, "year": y["window_end"][:4], "tracks": got})

    os.makedirs(os.path.dirname(YEARLY_TRACKS_OUT), exist_ok=True)
    all_tracks = [t for y in out_years for t in y["tracks"]]
    with open(YEARLY_TRACKS_OUT, "w") as fh:
        json.dump({
            "center": [KBDU_LAT, KBDU_LON],
            "radius_nm": RADIUS_NM,
            "alt_max_ft": ALT_MAX_FT,
            "by_year": {y["year"]: {"years_back": y["years_back"], "count": len(y["tracks"])} for y in out_years},
            "tracks": all_tracks,
        }, fh, separators=(",", ":"))
    print(f"\nwrote {len(all_tracks)} total tracks -> {YEARLY_TRACKS_OUT}")
    if _last_remaining is not None:
        print(f"credits remaining: {_last_remaining}")

def track_for_retry(icao24, t):
    tr = track_for(icao24, t)
    if tr is not None:
        return tr
    # one backoff retry on failure
    print(f"    backoff 25s then retry…")
    time.sleep(25)
    return track_for(icao24, t)

def run_years(args):
    cid, _ = load_creds()
    if cid:
        print(f"using OAuth2 client {cid}")
    else:
        print("error: need credentials for --years mode"); sys.exit(1)

    now = int(time.time())
    yearly = []
    # Year 0 = the current week (ending now). Years 1..N = same week N years back.
    for i in range(0, args.years + 1):
        center = now - i * 365 * 86400
        label = datetime.datetime.utcfromtimestamp(center).strftime("%Y-%m-%d")
        print(f"\n=== sample year-{i} ending {label} (delay {args.delay}s between calls) ===")
        arr, dep = sample_week(args.airport, center, delay=args.delay)
        print(f"  arrivals {len(arr)}  departures {len(dep)}")

        # Normalize + keep only fields useful for noise analysis
        rows = []
        seen = set()
        for f, direction in [(x, "arrival") for x in arr] + [(x, "departure") for x in dep]:
            key = (f.get("icao24"), f.get("firstSeen"), direction)
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "icao24": f.get("icao24"),
                "call": (f.get("callsign") or "").strip(),
                "direction": direction,
                "firstSeen": f.get("firstSeen"),
                "lastSeen": f.get("lastSeen"),
                "estDepAp": f.get("estDepartureAirport"),
                "estArrAp": f.get("estArrivalAirport"),
            })

        yearly.append({
            "years_back": i,
            "window_end": label,
            "window_begin": datetime.datetime.utcfromtimestamp(center - 7 * 86400).strftime("%Y-%m-%d"),
            "arrivals": len(arr),
            "departures": len(dep),
            "unique_flights": len(rows),
            "unique_aircraft": len({r["icao24"] for r in rows if r["icao24"]}),
            "flights": rows,
        })

    os.makedirs(os.path.dirname(META_OUT), exist_ok=True)
    with open(META_OUT, "w") as fh:
        json.dump({"airport": args.airport, "years": yearly}, fh, separators=(",", ":"))

    print(f"\nwrote {META_OUT}")
    if _last_remaining is not None:
        print(f"credits remaining: {_last_remaining}")
    print(f"\n{'year':<6} {'window':<24} {'flights':>8} {'aircraft':>10}")
    for y in yearly:
        print(f"-{y['years_back']:<5} {y['window_begin']} .. {y['window_end']}  {y['unique_flights']:>8}  {y['unique_aircraft']:>10}")

def sample_week(airport, center_ts, delay=1.5):
    """Fetch arrivals + departures for a 7-day window ending at center_ts."""
    begin = center_ts - 7 * 86400
    end = center_ts
    arr, dep = [], []
    for d in range(7):
        b = begin + d * 86400
        e = b + 86400
        arr += flights(airport, b, e, "arrival")
        time.sleep(delay)
        dep += flights(airport, b, e, "departure")
        time.sleep(delay)
    return arr, dep

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--airport", default="KBDU")
    ap.add_argument("--days", type=int, default=2)
    ap.add_argument("--max-tracks", type=int, default=100, help="cap on track/all calls")
    ap.add_argument("--years", type=int, default=0,
                    help="year-over-year mode: sample one week per year going back N years")
    ap.add_argument("--years-tracks", type=int, default=0,
                    help="fetch up to N tracks per year using existing flights_yearly.json")
    ap.add_argument("--delay", type=float, default=1.5,
                    help="seconds between API calls in --years mode")
    ap.add_argument("--track-delay", type=float, default=4.0,
                    help="seconds between /tracks/all calls in --years-tracks mode")
    args = ap.parse_args()

    try:
        if args.years > 0:
            return run_years(args)
        if args.years_tracks > 0:
            return run_years_tracks(args)
    except RateLimited as e:
        print("\n*** RATE LIMITED — aborting run ***")
        print(f"  endpoint: {e.url}")
        retry = e.retry_after
        if retry is None:
            print("  server did not send x-rate-limit-retry-after-seconds")
        elif retry > 86400 * 30:
            # Anything beyond ~30 days is a sentinel ("indefinite block")
            print(f"  retry-after = {retry} (sentinel — effectively an indefinite block)")
            print("  account may be hard-throttled; check https://opensky-network.org dashboard")
        else:
            when = datetime.datetime.now() + datetime.timedelta(seconds=retry)
            print(f"  server says retry in {retry} s ({retry/3600:.1f} h)")
            print(f"  earliest safe retry: {when.strftime('%Y-%m-%d %H:%M')}")
        sys.exit(2)

    now = int(time.time())
    begin = now - args.days * 86400

    cid, _ = load_creds()
    if cid:
        print(f"using OAuth2 client {cid}")
    else:
        print("warning: no credentials — anonymous access is blocked for historical")

    # /flights/arrival + /flights/departure have a max window of 7 days and
    # return up to ~500 flights per call. Chunk by day to stay under limits.
    arrivals, departures = [], []
    for d in range(args.days):
        b = begin + d * 86400
        e = b + 86400
        arrivals += flights(args.airport, b, e, "arrival")
        departures += flights(args.airport, b, e, "departure")
        time.sleep(0.5)  # be polite

    print(f"\narrivals {len(arrivals)}  departures {len(departures)}")

    all_flights = []
    seen = set()
    for f in arrivals + departures:
        key = (f.get("icao24"), f.get("firstSeen"))
        if key in seen:
            continue
        seen.add(key)
        all_flights.append(f)

    tracks = []
    for i, f in enumerate(all_flights[: args.max_tracks]):
        icao24 = f.get("icao24")
        t = f.get("lastSeen") or f.get("firstSeen")
        if not icao24 or not t:
            continue
        print(f"  [{i+1}/{min(len(all_flights), args.max_tracks)}] track {icao24} call={f.get('callsign','').strip()}")
        tr = track_for(icao24, t)
        time.sleep(0.3)
        if not tr or not tr.get("path"):
            continue
        pts = []
        for row in tr["path"]:
            # path row: [time, lat, lon, baro_alt_m, true_track, on_ground]
            _ts, lat, lon, alt_m, _trk, on_ground = row
            if lat is None or lon is None or on_ground:
                continue
            if alt_m is None:
                continue
            alt_ft = int(alt_m * 3.28084)
            if alt_ft <= 0 or alt_ft >= ALT_MAX_FT:
                continue
            if nm_from_kbdu(lat, lon) > RADIUS_NM:
                continue
            pts.append([round(lat, 5), round(lon, 5), alt_ft])
        if len(pts) >= 3:
            tracks.append({
                "call": (f.get("callsign") or "").strip() or icao24,
                "type": "",
                "src": f"opensky/{icao24}",
                "points": pts,
            })

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as fh:
        json.dump({
            "center": [KBDU_LAT, KBDU_LON],
            "radius_nm": RADIUS_NM,
            "alt_max_ft": ALT_MAX_FT,
            "days": args.days,
            "tracks": tracks,
        }, fh, separators=(",", ":"))

    pts_total = sum(len(t["points"]) for t in tracks)
    print(f"\nwrote {len(tracks)} tracks ({pts_total} points) -> {os.path.relpath(OUT)}")

if __name__ == "__main__":
    main()
