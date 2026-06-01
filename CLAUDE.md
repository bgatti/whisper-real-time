# FlightSafe / Noise KBDU

This is a noise-monitoring and glider-ops web application for KBDU (Boulder Municipal Airport). Vite + React frontend, Node.js API middleware, Postgres in production, JSON files locally.

**Deploying?** See [.claude/deploy.md](.claude/deploy.md) for Railway commands, the `--path-as-root` gotcha, and rollback steps.

## ADS-B Server API

The ADS-B layer sits in `noise/web/adsb.js` (core logic) and `noise/web/vite.config.js` (HTTP endpoints inside `adsbApiPlugin()`). Config files live in `noise/web/data/fleet.json` and `noise/web/data/zones.json`. Tests are in `noise/web/adsb.test.js`.

All endpoints return JSON with `Access-Control-Allow-Origin: *`.

- **Production**: https://web-app-production-fedf.up.railway.app
- **Local dev**: http://localhost:5174

---

### Live Positions

```
GET /api/adsb/live
```

Current position for every tracked aircraft (all 600+ in the capture radius, not just fleet).

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `icao` | string | — | Comma-separated ICAO hex codes to filter (e.g. `?icao=a59663,a5f99b`) |

Response:
```json
{
  "aircraft": [{
    "icao": "a59663",
    "tail": "N4593Y",
    "lat": 40.04,
    "lon": -105.22,
    "alt_ft": 6500,
    "gs_kts": 85,
    "track_deg": 270,
    "vs_fpm": 500,
    "squawk": null,
    "last_seen_s": 3
  }]
}
```

Groundspeed, track, and vertical speed are derived from the last two ADS-B points. `last_seen_s` is seconds since the most recent position update.

---

### Aircraft Track

```
GET /api/adsb/track/:icao
```

Full position history and detected flight phases for one aircraft.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `since` | ISO timestamp | 4 hours ago | Only return points after this time |

Response:
```json
{
  "icao": "a59663",
  "tail": "N4593Y",
  "points": [{ "ts": "...", "lat": 40.04, "lon": -105.22, "alt": 6500, "gs": null, "vs": null }],
  "phases": [{
    "type": "climbing_on_tow",
    "start_ts": "...",
    "end_ts": "...",
    "alt_start": 5500,
    "alt_end": 7300
  }]
}
```

Phase types: `on_ground`, `taxiing`, `climbing_on_tow`, `descending`. The ground/airborne boundary is field elevation (5288 ft) + 150 ft AGL. Airborne segments are split at peak altitude into a climb phase and a descent phase.

Returns 404 if the ICAO hex is not in today's live track data.

---

### Completed Flights (Tow Cycles)

```
GET /api/adsb/flights
```

Extracted tow cycles — each represents one takeoff-climb-release-descend-land loop.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `tail` | string | — | Filter by tail number (e.g. `N4593Y`). Without this, only fleet aircraft are returned. |
| `from` | date | — | Start date filter (`YYYY-MM-DD`) |
| `to` | date | — | End date filter (`YYYY-MM-DD`) |

Response:
```json
{
  "flights": [{
    "id": "a59663-1700000030000",
    "icao": "a59663",
    "tail": "N4593Y",
    "date": "2026-04-19",
    "takeoff_ts": "2026-04-19T14:00:30.000Z",
    "release_ts": "2026-04-19T14:06:00.000Z",
    "landing_ts": "2026-04-19T14:09:00.000Z",
    "release_alt_ft": 2012,
    "climb_rate_fpm": 450,
    "cycle_time_min": 8.5,
    "operator": "Soaring Society of Boulder",
    "role": "tow",
    "phases": [{ "type": "climbing_on_tow", "start_ts": "...", "end_ts": "...", "alt_start": 5500, "alt_end": 7300 }]
  }]
}
```

`release_alt_ft` is AGL (MSL minus field elevation). `cycle_time_min` is total ground-to-ground. In-progress flights have `landing_ts: null` and `cycle_time_min: null`. Flight IDs are deterministic: `{icao}-{takeoff_epoch_ms}`.

---

### Flight Track

```
GET /api/adsb/flights/:id/track
```

Position points for a specific completed flight. The `:id` comes from the flights endpoint above.

Response:
```json
{
  "id": "a59663-1700000030000",
  "icao": "a59663",
  "tail": "N4593Y",
  "points": [{ "ts": "...", "lat": 40.04, "lon": -105.22, "alt": 6500 }]
}
```

Only resolves flights from fleet aircraft (those in `data/fleet.json`). Returns 404 for non-fleet flight IDs.

---

### Aggregated Stats

```
GET /api/adsb/stats
```

Performance statistics aggregated across completed tow cycles.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `tail` | string | — | Filter to one tail |
| `from` | date | — | Start date |
| `to` | date | — | End date |
| `group_by` | string | `all` | One of: `all`, `da_band`, `hour`, `glider` |

Response:
```json
{
  "groups": [{
    "key": "all",
    "count": 42,
    "avg_cycle_min": 8.5,
    "avg_climb_fpm": 450,
    "p10_cycle": 6.2,
    "p50_cycle": 8.3,
    "p90_cycle": 11.1
  }]
}
```

DA bands: `<6000`, `6000-7000`, `7000-8000`, `8000-9000`, `9000+`. Hour keys are UTC like `14:00`. Incomplete flights (null cycle time) are excluded.

---

### Active Tow Plane State

```
GET /api/adsb/active-tow
```

Real-time state of fleet tow planes with ETA predictions and glider pairing.

Response:
```json
{
  "tow_planes": [{
    "tail": "N4593Y",
    "icao": "a59663",
    "phase": "climbing_on_tow",
    "current_alt_ft": 6500,
    "climb_rate_fpm": 500,
    "est_release_ts": "2026-04-19T14:06:00.000Z",
    "est_available_ts": "2026-04-19T14:09:30.000Z",
    "current_cycle_start_ts": "2026-04-19T14:00:00.000Z",
    "paired_glider_tail": "N505PB",
    "paired_glider_icao": "bbb222"
  }]
}
```

`est_release_ts` is predicted from current altitude + climb rate to 2000 ft AGL. `est_available_ts` adds descent (~800 fpm) + 60s taxi. Glider pairing uses ADS-B proximity: two aircraft within 0.1 nm laterally and 300 ft vertically, where one is a fleet tow plane and the other is not.

Only returns tow planes that have recent ADS-B data. Empty array means no tow planes are currently tracked.

---

### Fleet Config

```
GET /api/adsb/config/fleet
PUT /api/adsb/config/fleet
```

ICAO hex-to-tail mapping. Determines which aircraft are tracked as tow planes.

```json
{
  "a59663": { "tail": "N4593Y", "type": "PA25", "operator": "Soaring Society of Boulder", "role": "tow" },
  "a5f99b": { "tail": "N4785F", "type": "PA18", "operator": "Soaring Society of Boulder", "role": "tow" },
  "a4e8b7": { "tail": "N4337Y", "type": "PA25", "operator": "Mile High Gliding", "role": "tow" }
}
```

PUT replaces the entire config. Invalidates the flights cache.

---

### Zone Config

```
GET /api/adsb/config/zones
PUT /api/adsb/config/zones
```

Airport geofence and phase-detection thresholds.

```json
{
  "airport": "KBDU",
  "lat": 40.0394,
  "lon": -105.2258,
  "field_elevation_ft": 5288,
  "pattern_radius_nm": 2,
  "runway_heading": 8,
  "ground_speed_max_kts": 30,
  "climb_threshold_fpm": 200,
  "descent_threshold_fpm": -200,
  "release_alt_min_ft": 500,
  "release_alt_max_ft": 3500,
  "on_ground_alt_agl_ft": 150
}
```

`on_ground_alt_agl_ft` is added to `field_elevation_ft` to get the ground ceiling (5438 ft MSL). Points below that are ground phases; above are airborne.

---

### WebSocket Stream

```
WS /api/adsb/stream
```

Connect via `new WebSocket('ws://localhost:5174/api/adsb/stream')`. Receives JSON messages every 2 seconds for fleet aircraft only:

```json
{ "type": "position", "icao": "a59663", "tail": "N4593Y", "lat": 40.04, "lon": -105.22, "alt": 6500, "vs": 500, "gs": null }
```

No subscription handshake needed — connect and receive.

---

### Flight Impact — "Good Neighbor Score"

Scores completed flights on how gentle they were on the community below and serves a kiosk-ready feed of recent landings. Core scoring lives in `noise/web/flightScore.js` (pure, node-safe, unit-tested in `noise/web/flightScore.test.js`); HTTP endpoints are in `flightImpactPlugin()` in vite.config.js. **A higher score is a more considerate flight** (0–100). All wording is carrot-only — no penalties, just encouragement.

The score blends four tested inputs, then maps to 0–100 where 0 load → 100:
1. **Ground noise × population density** — the noise kernel (mirrors `noise/noise_heatmap.py`) summed over a local grid, each cell weighted by `population_density.json` people-per-km².
2. **Community voices** — `data/complaints.json` entries for that tail whose window overlaps the flight (severity-weighted yellow/orange/red).
3. **Neighborhood overlap** — feet and seconds spent low over a noise-abatement area, via `src/geo.js` `trackLengthFt`/`classifyPoint` (the same logic the live map uses).
4. **Divided by total trip time** — a longer, calmer flight is never penalised for staying aloft.

Aircraft are greeted as **home** (in `data/fleet.json`, or ≥2 landings here in the live window) vs **visitor** ("Welcome home, N…" vs "Welcome, N… — great to have you visiting KBDU").

```
GET /api/adsb/impact?airport=KBDU[&minutes=30]
```

Recent landings (default last 30 min; `minutes` clamped to 720) that touched down within 3 nm of `airport`. Omit `airport` for all nearby fields.

```json
{
  "airport": "KBDU", "window_minutes": 30, "generated_at": "2026-05-20T17:36:07.544Z",
  "count": 2,
  "flights": [{
    "id": "abfa60-1779298186576", "tail": "N871DH", "type": "FOX", "airport": "KBDU",
    "landed_ts": "2026-05-20T17:33:18.747Z", "trip_minutes": 3.5,
    "score": 77, "tier": "Silver", "home": true,
    "greeting": "Welcome home, N871DH! 🛬",
    "gentleness": { "quiet_skies": 83, "altitude_generosity": 89, "neighbor_harmony": 100 },
    "highlights": ["A calm flight — the community had nothing but quiet to report"],
    "detail": {
      "community_exposure": 8453922418, "peak_ground_db": 85.1, "voices_heard": 0,
      "considerate_path": { "gentle_seconds": 210, "close_seconds": 0, "total_seconds": 210, "feet_in_zone": 0 }
    }
  }]
}
```

`tier` is `Gold` (≥90) / `Silver` (≥75) / `Bronze` (≥55) / `Rising`. `gentleness` holds the three 0–100 sub-scores. Only flights with a `landing_ts` (terminated cycles) are scored.

```
GET /api/adsb/impact/:id
```

Full detail for one flight: everything in the summary plus `path` (`[{lat,lon,alt,ts}]`), `zones` (`[{name,polygon}]`), and `impact_overlay` (`{ bounds, w, h, db[] }` — the ground-noise dB grid, row 0 = south; null cells are quiet). Resolves flights landed in the last ~6 h.

```
GET /api/adsb/impact/:id/frame
```

Self-contained **embeddable** HTML (Leaflet from CDN) — drop in an `<iframe>`. Paints the flight path, the ground-noise heat overlay, the noise-abatement polygons, and a score badge with the greeting and highlights.

```
GET /kiosk/impact
```

Self-contained kiosk page: pick an airport + window, see recent landings as scored cards (home/visitor, tier ring, highlights, link to the map frame). Auto-refreshes every 30 s.

---

## Noise Exposure — "what does this point on the ground hear?"

Per-listener noise picture for any lat/lon over a time window. Walks
historical + live ADS-B tracks, filters to flights whose paths came
within `radius_nm` of the listener, scores each pass with the same
noise kernel as the Good Neighbor heatmap (via `dbAtListener()` in
`noise/web/flightScore.js`), and aggregates to a histogram + breakdowns
by purpose / operator / base / type / local hour.

A pass is a contiguous run of in-radius points with no gap > 5 min, so
one aircraft that touch-and-goes the listener three times produces three
event rows. Engineless aircraft (gliders, balloons) are skipped — they
contribute zero noise on their own (the tow plane is the source).

### List exposure events

```
GET /api/noise/exposure?lat=<float>&lon=<float>
```

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `lat` | float | — | Listener latitude (WGS-84). Required. |
| `lon` | float | — | Listener longitude. Required. |
| `radius_nm` | float | 5 | Audible-range cap. Clamped to [0.5, 20]. |
| `hours` | int | 24 | Rolling lookback window in hours (1..168). Ignored when `from`/`to` is set. |
| `from` | ISO datetime | — | Window start (paired with `to`). |
| `to` | ISO datetime | now | Window end. |
| `elev_ft` | float | nearest field elev | Listener terrain elevation MSL. |
| `db_floor` | float | — | Drop events with peak dBA below this. |
| `bins` | csv | `35,40,45,50,55,60,65,70,75,80,85,90` | Histogram bin edges (ascending). |

Response:
```json
{
  "listener": { "lat": 40.005, "lon": -105.205, "elev_ft": 5288, "radius_nm": 5 },
  "window": { "from": "2026-05-31T04:00:00Z", "to": "2026-06-01T04:00:00Z", "hours": 24 },
  "summary": {
    "total_events": 170,
    "peak_db": 54.0, "peak_tail": "N8141Y", "peak_type": "BE33", "peak_ts": 1780257123456,
    "mean_db": 18.4, "median_db": 14.1,
    "db_floor": null
  },
  "histogram": {
    "bins":   [35, 40, 45, 50, 55, 60, 65, 70, 75, 80, 85, 90],
    "counts": [11,  7,  9,  2,  0,  0,  0,  0,  0,  0,  0,  0]
  },
  "by_purpose":  [{ "key": "ga_single", "count": 43, "peak_db": 54.0, "mean_db": 18.6 }],
  "by_operator": [{ "key": "JOURNEYS AVIATION INC", "count": 11, "peak_db": 49.6, "mean_db": 17.2 }],
  "by_base":     [{ "key": "KBDU", "count": 62, "peak_db": 49.8, "mean_db": 16.0 }],
  "by_type":     [{ "key": "C172", "count": 38, "peak_db": 49.6, "mean_db": 14.8 }],
  "by_hour_local": [{ "hour_local": 0, "count": 0, "peak_db": null }, "... 24 buckets total"],
  "events": [{
    "hex": "a59663", "tail": "N4593Y", "type": "PA25",
    "operator": "Soaring Society of Boulder", "base": "KBDU", "purpose": "tow_plane",
    "pass_index": 0,
    "est_db": 49.8,
    "ts_at_closest": 1780257100000,
    "dist_ft": 2632, "alt_agl_ft": 2788, "slant_ft": 3833
  }]
}
```

Counts in `histogram.counts[i]` are events with `est_db ∈ [bins[i], bins[i+1])`; the last bucket is open-ended (`≥ bins[last]`). Events below the lowest bin are dropped from the histogram but remain in `events[]`. `by_hour_local` is always 24 buckets in `America/Denver` (DST-aware).

`est_db` is LMax — the loudest single segment at the listener. Same constants as `buildImpactGrid` (`REF_SOURCE_DB = 95`, `SPREAD_EXP = 25`, `G_TERRAIN = 0.65`, `ALPHA_ATM = 0.0016`), evaluated only at the listener point. `dbAtListener` defaults `dtS` to the real sample interval (clamped to [1, 30] s) rather than `buildImpactGrid`'s hardcoded `dtS=1`, which would under-report doseDb by 7–10 dB on the typical 5–10 s ADS-B cadence.

`Cache-Control: public, max-age=30`.

### Drill into one flight

```
GET /api/noise/exposure/flight/:hex?lat=<float>&lon=<float>
```

Per-segment dB trace at the listener for one aircraft. Use this to plot the `dB(t)` curve of a specific pass after picking it from the list above.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `lat`, `lon` | float | — | Listener position. Required. |
| `pass` | int | 0 | Which pass to return (chronological, 0-indexed). |
| `hours` | int | 24 | Rolling lookback (1..168). |
| `radius_nm` | float | 5 | Pass-segmentation radius (match what you used in the parent call). |
| `elev_ft` | float | nearest field elev | Listener terrain elevation. |

Response:
```json
{
  "hex": "a59663", "tail": "N4593Y", "type": "PA25",
  "listener": { "lat": 40.005, "lon": -105.205, "elev_ft": 5288 },
  "pass_index": 0, "pass_count": 3,
  "peak": {
    "peakDb": 49.8, "peakTs": 1780257100000,
    "closestSlantFt": 3833, "closestHorizFt": 2632, "closestAglFt": 2788,
    "closestLat": 40.012, "closestLon": -105.2, "closestAltFt": 8076, "closestTs": 1780257098000
  },
  "samples": [
    { "ts": 1780257090000, "db": 41.2, "dist_ft": 6210, "alt_agl_ft": 2745 },
    { "ts": 1780257100000, "db": 49.8, "dist_ft": 2632, "alt_agl_ft": 2788 }
  ]
}
```

`samples` has one entry per segment in the pass (each is that segment's peak dB + closest-approach geometry). Returns 404 if the hex has no points in the window or if `pass` is out of range.

---

## Noise Abatement Zones

```
GET /api/noise-zones[?airport=KBDU]
```

Voluntary noise abatement polygons with their upper altitude ceiling. Source data lives in `noise/web/src/noiseZones.js` (auto-generated from KML by `noise/import_kml.py`).

Response:
```json
{
  "count": 9,
  "default_ceiling_ft": 7500,
  "zones": [{
    "name": "KBDU Frasier Meadows",
    "airport": "KBDU",
    "note": "SE residential — Frasier Meadows / Keewaydin neighborhood",
    "ceiling_ft": 7500,
    "polygon": [[40.014271, -105.216554], [39.99606, -105.216639], ...]
  }]
}
```

`airport` is derived from the zone name's first whitespace-delimited token. `ceiling_ft` defaults to the global `ALT_THRESHOLD_FT = 7500 MSL` from `src/geo.js` — overrides can be set per-zone or per-airport in `noiseZonesApiPlugin()` in vite.config.js when specific airports publish their own ceilings. Polygons are `[[lat, lon], ...]` with the first/last vertices duplicated.

`Cache-Control: public, max-age=3600` — these are static config; bust by appending `?v=...`.

---

## Noise Reports — Audio Attachments

Saved noise reports can have MP3 audio clips attached (e.g. a 10-second spliced sample and a 5-second loudest excerpt). Audio is stored separately from the JSON report body so list endpoints stay small.

### Upload

```
POST /api/noise-reports/:id/audio/:slot
Content-Type: audio/mpeg
Body: raw MP3 bytes (no multipart, no base64)
```

| Slot | Purpose |
|------|---------|
| `spliced10s` | The 10-second spliced sample around the event |
| `loudest5s`  | The 5-second loudest excerpt |

Constraints: `Content-Type` must be `audio/mpeg` (or `audio/mp3`). Body capped at 1 MB. Re-uploads to the same `(reportId, slot)` replace the prior bytes.

Response: `201 { ok: true, reportId, slot, bytes }`. Errors: `400` (invalid slot or empty body), `413` (>1 MB), `415` (wrong Content-Type).

Typical client flow:
```js
// 1. Create the report (existing POST)
const { id } = await fetch('/api/noise-reports', {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(reportMeta),
}).then(r => r.json())

// 2. Attach audio clips (parallel)
await Promise.all([
  fetch(`/api/noise-reports/${id}/audio/spliced10s`, {
    method: 'POST', headers: { 'Content-Type': 'audio/mpeg' }, body: spliced10sBlob,
  }),
  fetch(`/api/noise-reports/${id}/audio/loudest5s`, {
    method: 'POST', headers: { 'Content-Type': 'audio/mpeg' }, body: loudest5sBlob,
  }),
])
```

### Playback

```
GET /api/noise-reports/:id/audio/:slot
```

Returns `audio/mpeg` bytes with `Cache-Control: public, max-age=31536000, immutable`. `404` if no clip exists for that slot. Use directly as an `<audio src="...">` source.

### Storage

- **Postgres**: `noise_audio` table — `(report_id, slot)` PK, `bytes BYTEA`, `mime`, `created_at`. Auto-created on first write.
- **Local dev**: `noise/web/data/audio/{reportId}/{slot}.mp3`.

The report JSON itself doesn't need to reference the audio — slot existence is implied by the URL pattern. For rendering, the client checks `GET .../audio/spliced10s` and shows the player if it returns 200.

---

## Key implementation details

- **Data source**: Live ADS-B positions are polled every 2s from `api.adsb.lol` by `liveCapturePlugin()` in vite.config.js and stored in `public/tracks_live.json` (local) or the `live_tracks` Postgres table (Railway). The ADS-B API reads from this same source.
- **Phase detection** (`adsb.detectPhases`): Walks the point array, classifying below-ceiling segments as ground/taxi and above-ceiling as climb/descent, splitting at peak altitude.
- **Tow cycle extraction** (`adsb.extractTowCycles`): Groups phases into complete takeoff-to-landing cycles. Each ground→climb→descent→ground sequence becomes one flight record.
- **Glider pairing** (`adsb.pairTowWithGliders`): Scans all live tracks for non-fleet aircraft within 0.1 nm / 300 ft of a climbing fleet tow plane. Stale points (>30s) are ignored.
- **Flight IDs** are deterministic: `{icao_hex}-{takeoff_epoch_ms}`. Same track data always produces the same ID.
- **Flights cache** has a 10s TTL. Config PUT endpoints invalidate it.

## Tests

```bash
npm test                # unit tests only (no server needed)
npm run test:live       # unit + integration against http://localhost:5174
```

Test file: `noise/web/adsb.test.js` — 43 unit tests + 15 integration tests.
