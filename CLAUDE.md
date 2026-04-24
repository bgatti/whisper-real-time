# FlightSafe / Noise KBDU

This is a noise-monitoring and glider-ops web application for KBDU (Boulder Municipal Airport). Vite + React frontend, Node.js API middleware, Postgres in production, JSON files locally.

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
