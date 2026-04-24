# KBDU Noise API

Small REST endpoint exposed by the Vite dev server for other services
(Slack bots, compliance dashboards, scripted notices, etc.) to query
KBDU noise-abatement offenses by aircraft.

## Endpoint

```
GET  http://localhost:5174/api/offenses
```

CORS is wide open (`Access-Control-Allow-Origin: *`) so it's safe to call
from any origin during development.

## Query parameters

| Name | Required | Format | Example |
|---|---|---|---|
| `tail` | yes | N-number, exact match | `N12JA` |
| `from` | no | `YYYY-MM-DD` inclusive | `2025-01-01` |
| `to`   | no | `YYYY-MM-DD` inclusive | `2025-12-31` |

Omit `from`/`to` to query every observed day for that tail.

## Response shape

```json
{
  "tail": "N12JA",
  "type": "Pipistrel Alpha Trainer 80HP (2023)",
  "school": "Journeys Aviation",
  "base": "KBDU",
  "window": { "from": "2025-01-01", "to": "2025-12-31" },
  "tracks_seen": 12,
  "total_offenses": 47,
  "worst": "orange",
  "offenses": [
    {
      "date": "2025-07-15",
      "worst": "orange",
      "zone": "Central & West Boulder",
      "points": 6,
      "peakAlt": 6200
    }
  ],
  "landing_url": "http://localhost:5174/?tail=N12JA#map"
}
```

### Field meanings

- **`tracks_seen`** — number of per-day flight tracks matched for this
  aircraft in the window (each track = one day's ADS-B recording)
- **`total_offenses`** — number of contiguous in-zone violation events
  across all matched tracks
- **`worst`** — most severe classification ever observed. `null` when
  no offenses were found. `yellow < orange < red`
- **`offenses[]`** — chronological list of events. Each event is a
  contiguous run of points classified as yellow/orange/red:
  - `date` — YYYY-MM-DD of the day capture
  - `worst` — worst band observed within the event
  - `zone` — name of the nearest noise-abatement polygon at the peak
  - `points` — number of ADS-B points in the event
  - `peakAlt` — MSL altitude (ft) at the peak-severity point
- **`landing_url`** — deep link to the map page with this aircraft
  pre-selected and year filter set to `all`. Opening it loads only that
  aircraft's full trajectory so whoever gets the notice can see the
  pattern immediately.

## Classification rules

Same as the live map. A point is flagged only when **both** dimensions
agree — altitude below 7500 MSL **and** inside a noise-abatement zone.
The result is the **less severe** of the two per-dimension classes so
high aircraft in a zone stay clean; low aircraft outside a zone stay
clean; only the co-occurrence is flagged.

Altitude bands (below 7500 MSL):
- yellow: within 250 ft (7250–7750)
- orange: 250–500 ft below (7000–7250)
- red: > 500 ft below (< 7000)

Zone bands (distance from nearest zone edge):
- yellow: within 250 ft of the boundary (either side)
- orange: 250–500 ft inside
- red: > 500 ft inside

## Usage examples

```bash
# All N12JA offenses, ever recorded
curl 'http://localhost:5174/api/offenses?tail=N12JA'

# Just 2025
curl 'http://localhost:5174/api/offenses?tail=N12JA&from=2025-01-01&to=2025-12-31'

# This calendar month
curl 'http://localhost:5174/api/offenses?tail=N12JA&from=2026-04-01&to=2026-04-30'
```

### Building a notice from the response

```bash
curl -s 'http://localhost:5174/api/offenses?tail=N12JA' | jq -r '
  "Aircraft: \(.tail) \(.school // "(unaffiliated)")",
  "Window: \(.window.from // "all") → \(.window.to // "all")",
  "Total offenses: \(.total_offenses) (worst: \(.worst // "clean"))",
  "Review the full track: \(.landing_url)"
'
```

### Sample JavaScript

```js
const res = await fetch(
  `http://localhost:5174/api/offenses?tail=${tail}&from=${from}&to=${to}`
)
const data = await res.json()
if (data.total_offenses > 0) {
  // attach data.landing_url in a Slack message / email / compliance log
}
```

## Notes

- **Dev-server only.** The endpoint lives in `vite.config.js` as a
  `configureServer` middleware. It only exists while `npm run dev` is
  running. For production deployment, port the handler into an Express /
  Fastify server that reads the same `tracks_yearly.json`.
- **Data freshness.** The handler re-reads `tracks_yearly.json` on every
  request, so the moment overnight ingestion writes a new batch the API
  sees it. `NOISE_ZONES` is cached in memory (module-level) since it's
  small and static; restart the dev server if you edit `noiseZones.js`.
- **Tail matching is case-sensitive** and exact — `N12JA` works,
  `n12ja` or `N12JA ` (trailing space) won't. Callers should uppercase
  and trim before sending.
