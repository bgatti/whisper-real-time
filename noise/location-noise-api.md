# Location Noise API

> What did **this point on the ground** hear, and from whom?

A pair of HTTP endpoints that answer "what is my noise exposure?" for an
arbitrary lat/lon. The agent prompt and per-listener Markdown report
spec live in [Location_noise report.md](Location_noise%20report.md); this
document covers the **machine API** the agent (and any client) calls.

- **Production**: <https://web-app-production-fedf.up.railway.app>
- **Local dev**: <http://localhost:5174>
- All responses are JSON with `Access-Control-Allow-Origin: *`.

---

## Why it exists

Before these endpoints, getting the listener-centric picture meant:

1. Pull `/api/flights/current` per airport.
2. For each tail, pull `/api/adsb/track/:icao`.
3. Walk every point, compute closest-approach, project into the noise
   kernel from [noise/web/flightScore.js](web/flightScore.js).
4. Aggregate by purpose / operator / base / hour.

That's the worked example in
[Location_noise report.md](Location_noise%20report.md#worked-example) —
N round-trips and a re-implementation of the noise kernel client-side.

`/api/noise/exposure` does all of that server-side in one request, with
the same noise kernel as the Good Neighbor heatmap, and returns both
the per-event rows and the aggregates ready to render.

The companion `/api/noise/exposure/flight/:hex` returns the per-segment
`dB(t)` trace for a single aircraft pass — drill-down after the user
picks a row from the list.

---

## Endpoints

### `GET /api/noise/exposure`

Enumerate every flight pass that came within audible range of a
listener over a time window. Returns per-event rows + a histogram +
breakdowns.

| Param | Type | Default | Notes |
| --- | --- | --- | --- |
| `lat` | float | — | Listener latitude (WGS-84). **Required.** |
| `lon` | float | — | Listener longitude. **Required.** |
| `radius_nm` | float | `5` | Audible-range cap. Clamped to `[0.5, 20]`. 5 nm captures low-altitude GA; 10 nm captures cruise jets. |
| `hours` | int | `24` | Rolling lookback in hours (1..168). Ignored when `from`/`to` is set. |
| `from` | ISO datetime | — | Window start. |
| `to` | ISO datetime | now | Window end. Must be > `from`. |
| `elev_ft` | float | nearest field elev | Listener terrain elevation MSL. Defaults to the nearest Front Range airport (KBDU 5288, KBJC 5673, …). |
| `db_floor` | float | — | Drop events with peak dBA below this. Applied **after** the histogram (so the histogram always reflects the full bin range). |
| `bins` | csv | `35,40,45,50,55,60,65,70,75,80,85,90` | Histogram bin edges (ascending). The last bin is open-ended. |

`Cache-Control: public, max-age=30` — the Railway edge caches the same
query for 30 s. Bust with any throwaway param (`&_=$RANDOM`) when
exploring interactively.

#### Response

```json
{
  "listener": { "lat": 40.005, "lon": -105.205, "elev_ft": 5288, "radius_nm": 5 },
  "window": {
    "from": "2026-05-31T04:00:00.000Z",
    "to":   "2026-06-01T04:00:00.000Z",
    "hours": 24
  },
  "summary": {
    "total_events": 170,
    "peak_db": 54.0,
    "peak_tail": "N8141Y",
    "peak_type": "BE33",
    "peak_ts": 1780238697518,
    "mean_db": 18.5,
    "median_db": 13.9,
    "db_floor": null
  },
  "histogram": {
    "bins":   [35, 40, 45, 50, 55, 60, 65, 70, 75, 80, 85, 90],
    "counts": [11,  7,  9,  2,  0,  0,  0,  0,  0,  0,  0,  0]
  },
  "by_purpose":  [{ "key": "ga_single",            "count": 64, "peak_db": 54.0, "mean_db": 18.6 }],
  "by_operator": [{ "key": "JOURNEYS AVIATION INC", "count": 12, "peak_db": 49.8, "mean_db": 17.2 }],
  "by_base":     [{ "key": "KBDU",                  "count": 62, "peak_db": 49.8, "mean_db": 16.0 }],
  "by_type":     [{ "key": "C172",                  "count": 38, "peak_db": 49.6, "mean_db": 14.8 }],
  "by_hour_local": [
    { "hour_local": 0, "count": 0, "peak_db": null },
    "... 24 buckets total, America/Denver (DST-aware)"
  ],
  "events": [{
    "hex": "ab1aca",
    "tail": "N8141Y",
    "type": "BE33",
    "operator": "TRIFUNAC ALEXANDER D TRUSTEE, …",
    "base": "KBJC",
    "purpose": "ga_single",
    "pass_index": 0,
    "est_db": 54.0,
    "ts_at_closest": 1780238697518,
    "dist_ft": 157,
    "alt_agl_ft": 2362,
    "slant_ft": 2367
  }]
}
```

#### Field meanings

- `est_db` — **LMax** (the loudest single segment) at the listener.
  Same units and constants as the Good Neighbor heatmap's peak dB.
- `ts_at_closest` — UNIX milliseconds at the closest-approach
  point in the loudest segment.
- `dist_ft` — horizontal distance (feet) from the listener at the
  closest-approach point. `alt_agl_ft` is aircraft altitude minus
  `elev_ft`. `slant_ft = hypot(dist_ft, alt_agl_ft)`.
- `pass_index` — 0 for an aircraft's first pass through the radius,
  1 for its second, etc. A pass is a contiguous run of in-radius
  points with no time gap > 5 min. Useful when one tail does multiple
  touch-and-goes overhead.
- `purpose` — resolved via [noise/web/vite.config.js → `resolvePurpose()`](web/vite.config.js):
  special-use registry > type-unambiguous (PA25 = tow, GLID = glider) >
  curated tracks DB > type heuristic. See the
  [phase→purpose mapping](Location_noise%20report.md#phase--purpose-mapping).
- `operator` / `base` — three-signal resolution: `fleet.json` >
  tracks DB `own_op` / `base_airport` > school registry > visitor.

#### Edge cases

| Condition | Behaviour |
| --- | --- |
| Missing `lat` or `lon` | `400 { error: "lat and lon are required …" }` |
| `to <= from` | `400 { error: "from/to must be valid ISO …" }` |
| No flights in window | `200` with `total_events: 0`, empty `events[]`, zero histogram counts. Not an error. |
| Glider / balloon / engineless | Skipped — contributes zero noise on its own. The tow plane (PA25) is scored normally. |
| Hex without timestamps (legacy historical track) | Still included; `ts_at_closest` may be `null`, falls out of `by_hour_local`. |

---

### `GET /api/noise/exposure/flight/:hex`

Per-segment `dB(t)` trace at the listener for **one** aircraft —
intended as the drill-down after a user picks a row from the list
endpoint above.

| Param | Type | Default | Notes |
| --- | --- | --- | --- |
| `:hex` | path | — | ICAO hex (lowercased). Matches what `/api/adsb/live` emits. |
| `lat`, `lon` | float | — | Listener position. **Required.** |
| `pass` | int | `0` | Which pass to return (0-indexed, chronological). |
| `hours` | int | `24` | Rolling lookback (1..168). |
| `radius_nm` | float | `5` | Pass-segmentation radius — use the same value you passed to the parent call. |
| `elev_ft` | float | nearest field elev | Listener terrain elevation. |

#### Response

```json
{
  "hex": "ab1aca",
  "tail": "N8141Y",
  "type": "BE33",
  "listener": { "lat": 40.005, "lon": -105.205, "elev_ft": 5288 },
  "pass_index": 0,
  "pass_count": 1,
  "peak": {
    "peakDb": 54.0,
    "peakTs": 1780238697518,
    "closestSlantFt": 2367,
    "closestHorizFt": 157,
    "closestAglFt": 2362,
    "closestLat": 40.004898,
    "closestLon": -105.204454,
    "closestAltFt": 7650,
    "closestTs": 1780238697518,
    "silent": false
  },
  "samples": [
    { "ts": 1780238553268, "db": -34.3, "dist_ft": 28619, "alt_agl_ft":  837 },
    { "ts": 1780238697518, "db":  54.0, "dist_ft":   157, "alt_agl_ft": 2362 },
    { "ts": 1780238816069, "db": -36.2, "dist_ft": 27708, "alt_agl_ft": 3387 }
  ]
}
```

- `peak` is the same LMax + closest-approach geometry that
  `/api/noise/exposure` reports for this `(hex, pass_index)` row.
- `samples[]` has one entry per segment in the pass (so a 4-minute
  pass at 6 s sampling → ~40 samples). Each segment's `db` is the
  peak dB the listener heard during that segment.
- `silent: true` (engineless aircraft) → `peakDb` is `null` and
  `samples[].db` will all be `null`.

#### Edge cases

| Condition | Behaviour |
| --- | --- |
| `hex` has no points in window | `404 { error: "no track points for hex in window" }` |
| `pass` ≥ `pass_count` | `404 { error: "pass N not found (have M)" }` |

---

## The noise model

`est_db` is the output of
[`dbAtListener()`](web/flightScore.js) — a scalar form of
`buildImpactGrid()`. It mirrors the inner segment loop exactly, with
the same constants:

| Constant | Value | Role |
| --- | --- | --- |
| `REF_SOURCE_DB` | 95 | Source level at 100 ft slant for a 100 HP single |
| `HP_REF` | 100 | HP scaling reference |
| `SPREAD_EXP` | 25.0 | dB per decade of slant range (spherical spreading + atmosphere baked in) |
| `G_TERRAIN` | 0.65 | Ground-attenuation strength |
| `ALPHA_ATM` | 0.0016 | dB per ft of slant — atmospheric absorption |
| `MIN_SLANT_FT` | 50 | Slant range floor (avoids log singularity overhead) |

Per segment `a → b`:

```text
hpDb     = 10 · log10(hp / 100)
dz       = b.alt - a.alt
stateDb  = +0   if climbing   (dz > 50)
           -6   if descending (dz < -50)
           -3   if level
vKts     = (segDist_m / dtS) · 1.94384,   floored at 30 kts
doseDb   = 10 · log10(100 / max(vKts, 20))      # slower = more dose

srcDb    = REF_SOURCE_DB + hpDb + stateDb + doseDb
slantFt  = max(MIN_SLANT_FT, hypot(horizFt, aglFt))   # to listener
db_pt    = srcDb
         - SPREAD_EXP   · log10(slantFt / 100)        # spherical spread
         - G_TERRAIN    · (10 - 8·sin(θ))             # ground-grazing penalty
         - G_TERRAIN·9.5· cos(θ)^2.5                  # low-angle penalty
         - ALPHA_ATM    · slantFt                     # atmospheric absorption
         + 10 · log10(sin²ψ + 0.05)                   # directivity (broadside louder)
```

Where `θ` is the elevation angle from the listener to the aircraft
and `ψ` is the angle between the listener-direction and the
aircraft's heading.

### One important difference vs. `buildImpactGrid`

`buildImpactGrid` hardcodes `dtS = 1` (it assumes 1 Hz sampling).
ADS-B data on Railway is sampled every 5–10 s, which inflates `vKts`
5–10× and pulls `doseDb` 7–10 dB **too low**.

`dbAtListener` **derives `dtS` from real timestamps** (clamped to
`[1, 30]` s) so the numbers reflect the actual sample cadence. This
means the histogram peak from `/api/noise/exposure` for a given pass
will read 7–10 dB **higher** than the grid's `peakDb` for the same
flight — `dbAtListener` is the more physically meaningful number.

Pass `?dtS=1` (not currently exposed as a query param, but available
via direct calls to `dbAtListener`) to force the legacy behaviour for
side-by-side comparison.

### Why engineless types are skipped

The HP table only zeroes the generic `GLID` entry. Typed gliders
(`AS21`, `DG500`, …) would otherwise fall through to `DEFAULT_HP =
180` and be modelled as if they were powered trainers. `dbAtListener`
checks
[`isEnginelessType()`](web/src/geo.js)
first and returns `{ silent: true }` for any matching type. The tow
plane (`PA25`, `PA18`) is scored normally — that's where the noise
comes from on an aerotow.

---

## Examples

### Quiet residential listener, last 24 h

```bash
BASE="https://web-app-production-fedf.up.railway.app"
curl -s "$BASE/api/noise/exposure?lat=40.005&lon=-105.205&radius_nm=5&hours=24" \
  | python -c "import sys,json; d=json.load(sys.stdin); s=d['summary']; \
print(f\"events={s['total_events']} peak={s['peak_db']}dB \" \
      f\"({s['peak_tail']} {s['peak_type']}) mean={s['mean_db']} median={s['median_db']}\"); \
print('hist:', d['histogram']['counts'])"
```

```
events=170 peak=54.0dB (N8141Y BE33) mean=18.5 median=13.9
hist: [11, 7, 9, 2, 0, 0, 0, 0, 0, 0, 0, 0]
```

### Only events ≥ 45 dB

```bash
curl -s "$BASE/api/noise/exposure?lat=40.005&lon=-105.205&radius_nm=5&hours=24&db_floor=45"
```

### A specific 6-hour window

```bash
curl -s "$BASE/api/noise/exposure?lat=40.005&lon=-105.205&radius_nm=5\
&from=2026-05-31T14:00:00Z&to=2026-05-31T20:00:00Z"
```

### Drill into the loudest flight

```bash
# Get the peak event's hex from the parent call, then:
curl -s "$BASE/api/noise/exposure/flight/ab1aca?lat=40.005&lon=-105.205&pass=0" \
  | jq '.peak, (.samples | length), (.samples | [min_by(.db).db, max_by(.db).db])'
```

```
{ "peakDb": 54.0, "closestHorizFt": 157, "closestAglFt": 2362, ... }
45
[ -36.2, 54.0 ]
```

---

## Aggregations — what they mean

| Field | Computed how | Notes |
| --- | --- | --- |
| `histogram.counts[i]` | Events with `est_db ∈ [bins[i], bins[i+1])`. Last bin is `≥ bins[last]`. | Events below `bins[0]` are dropped from the histogram but stay in `events[]`. |
| `by_purpose` / `by_operator` / `by_base` / `by_type` | One row per distinct key. `count` is events; `peak_db` is the loudest single event in the group; `mean_db` is the arithmetic mean over events in the group. | Sorted by `count` descending. `null`/missing keys collapse to `"unknown"`. |
| `by_hour_local` | 24 buckets keyed by local hour (`hour_local`) in America/Denver. DST-aware (UTC-7 standard, UTC-6 daylight). | `peak_db` is the loudest event in that hour, `null` if no events. |
| `summary.peak_*` | From the row with the highest `est_db` in `events[]`. | When `total_events = 0`, all `peak_*` and stat fields are `null`. |

---

## Implementation notes

- Plugin: `noiseExposurePlugin()` in
  [noise/web/vite.config.js](web/vite.config.js), registered after
  `noiseZonesApiPlugin()`.
- Data sources, in order: historical aggregated tracks
  (`db.loadTracksFromDb`) for the bulk of the window, **plus** live
  tracks (`db.loadLiveFromDb`) for the last ~24 h so today's data
  before the nightly aggregation still surfaces.
- Tail → `(operator, base, purpose)` resolution is the same three-signal
  logic `/api/adsb/current-flights` uses (`fleet.json` → tracks DB
  `base_airport`/`own_op` → school fleet → visitor).
- Pass segmentation: `PASS_GAP_MS = 5 * 60 * 1000` — a gap longer than
  this inside the radius starts a new pass.
- Cache: 30 s on the response via `Cache-Control` (Railway edge
  honours it). No in-process cache — every uncached request walks the
  tracks list fresh.

---

## Tests

- Unit tests for `dbAtListener`:
  [`noise/web/flightScore.test.js`](web/flightScore.test.js) →
  `describe('dbAtListener')` (6 cases: monotonic in altitude, monotonic
  in HP, engineless silent, timestamp containment, empty input, agreement
  with `buildImpactGrid` when both pinned to `dtS=1`).
- Integration tests against a running dev server:
  [`noise/web/noiseExposure.test.js`](web/noiseExposure.test.js) — 8
  cases covering response shape, validation, `db_floor`, drill-down
  consistency. Run with:

  ```bash
  cd noise/web
  npx vite --port 5174 &
  ADSB_BASE=http://localhost:5174 npx vitest run noiseExposure.test.js
  ```

---

## See also

- [Location_noise report.md](Location_noise%20report.md) — agent prompt
  + per-listener Markdown report spec that consumes this API.
- [noise/web/flightScore.js](web/flightScore.js) — `dbAtListener`,
  `buildImpactGrid`, `scoreFlight`.
- [noise/web/src/geo.js](web/src/geo.js) — `distFt`, `classifyPoint`,
  `isEnginelessType`.
- [CLAUDE.md](../CLAUDE.md) — API contract for the wider project;
  has a short "Noise Exposure" section keyed to this doc.
