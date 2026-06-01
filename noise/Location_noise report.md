# Location Noise Report — agent prompt + spec

Given a geographic point (`lat`, `lon`) and a time window, produce a
comprehensive picture of the **noise that point experienced** — every
aircraft that passed within audible range, estimated dBA at the
listener, why each was there (training pattern / transit / practice
area / agricultural / etc.), and who operated it (school, club,
airport, fleet).

This document is both the **spec** for the report and the **prompt**
for the agent that produces it. The agent should read this file
end-to-end before issuing API calls.

---

## Input parameters

| Param | Required | Default | Meaning |
| --- | --- | --- | --- |
| `LAT` | yes | — | Listener latitude (WGS-84 decimal degrees) |
| `LON` | yes | — | Listener longitude |
| `WINDOW` | yes | last 24 h | ISO range like `2026-05-30T00:00:00Z..2026-05-31T00:00:00Z` |
| `RADIUS_NM` | no | 5 | Audible-range cap. 5 nm captures low-altitude GA; 10 nm captures cruise jets. |
| `ELEV_FT_MSL` | no | nearest field elev | Listener's terrain elevation for AGL calc. KBDU=5288, KBJC=5673, KAPA=5885. |

If the user gives an address, geocode locally (`geopy` /
`Nominatim`) — don't call out for that.

---

## Output

A Markdown report saved to
`noise/reports/noise_report_<LAT>_<LON>_<ISODATE>.md` containing:

1. **Executive summary** — 1 paragraph + key numbers:
   - Total flights with closest-approach within `RADIUS_NM`
   - Peak estimated dBA at the location + which aircraft + when
   - Peak hour-of-day (UTC + local)
   - Dominant noise source (aircraft type / operator / airport)
   - Comparison to a typical day at the nearest field
2. **Per-flight detail** — table, one row per nearby flight, columns:
   `ts_at_closest | tail | type | operator | base | airport | phase | alt_agl_ft | dist_ft | est_dba | purpose`
3. **Per-aircraft-type breakdown** — flights, flight-minutes,
   peak dBA, mean dBA by ICAO type code (with the human description
   from `TYPE_DESC` in [noise/web/vite.config.js](web/vite.config.js)).
4. **Per-operator breakdown** — same metrics by operator (flight
   school / glider club / unaffiliated GA / commercial / military).
5. **Per-airport breakdown** — where each flight took off or landed.
6. **Hourly pattern** — 24-bar table: audible flights per hour +
   cumulative dBA-seconds.
7. **Notable events** — top 5 loudest, top 5 lowest altitude over the
   listener, any noise-abatement-zone violations within
   `RADIUS_NM`.
8. **Methodology** — explicit assumptions: dBA formula, AGL calc,
   radius, time bins, where data was missing.

Print the **executive summary** + the **file path** to chat after
writing.

---

## APIs

Base URL: `https://web-app-production-fedf.up.railway.app`
All endpoints return JSON with `Access-Control-Allow-Origin: *`.

| Endpoint | Use for |
| --- | --- |
| `GET /api/flights/current?airport=<icao>&landed_hours=<N>&range_nm=25` | Primary source of nearby flights. `landed_hours` clamps to 168 h. Already-classified phase + school attribution. |
| `GET /api/adsb/track/:icao?since=<ISO>` | Per-flight position history. Returns `points[]` with `[ts, lat, lon, alt, gs, vs]`. Use to compute closest-approach geometry. |
| `GET /api/adsb/flights?from=<YYYY-MM-DD>&to=<YYYY-MM-DD>[&tail=<N…>]` | Historical completed cycles. Without `tail`, returns only fleet aircraft. |
| `GET /api/adsb/live` | All currently-tracked aircraft (~600). Use for "right now" snapshots only. |
| `GET /api/airports/:icao` | Runway centerlines, field elevation, magnetic variation, tz. |
| `GET /api/runways?center=<icao>&radius_nm=50` | Regional runway data — useful if listener is between several airports. |
| `GET /api/noise-zones?airport=<icao>` | VNAP polygons + ceilings. Flag flights that crossed them within `RADIUS_NM` of the listener. |
| `GET /api/excursions/boot?hours=24&limit=150` | Bootstrap data including `complaints.items[]` with geocoded lat/lon. Use to correlate community reports to specific flights. |
| `GET /api/adsb/impact?airport=<icao>&minutes=N` | Scored recent landings (Good Neighbor Score). Helpful for tier breakdown ("how considerate were the flights overhead?"). |

**Pagination strategy.** For windows > 24 h, call
`/api/flights/current` once per `landed_hours=24` chunk (or
once with `landed_hours=168` for a 7-day window — it clamps). Then,
for each unique `(tail, takeoff_ts)`, pull
`/api/adsb/track/{icao}?since=<takeoff_ts>` for the actual
position track.

---

## Noise model (reproduce locally)

The server's per-flight scoring lives in
[noise/web/flightScore.js](web/flightScore.js) — mirrors
[noise/noise_heatmap.py](noise_heatmap.py). For a listener at a
specific point, the relevant formula is:

```python
import math

def est_dba_at_listener(
    ac_type,          # ICAO type code e.g. "C172", "PA25", "PW6"
    alt_ft_msl,       # aircraft MSL altitude
    listener_elev_ft, # listener terrain elev MSL
    ac_lat, ac_lon, listener_lat, listener_lon,
):
    if is_engineless_type(ac_type):
        return 0.0  # glider/motorglider engine-off

    agl = max(alt_ft_msl - listener_elev_ft, 100)  # floor 100 ft

    base = TYPE_BASE_DBA.get(ac_type, 72)  # default trainer baseline

    # Vertical attenuation — -6 dB per altitude doubling above 1000 ft.
    # Mirrors the AGL-aware proxy used by /api/flights/current.
    vert = 6 * math.log2(agl / 1000) if agl > 1000 else 0

    # Lateral attenuation — point-source spherical spreading,
    # -3 dB per distance doubling beyond 500 ft slant range.
    dist_ft = geo_dist_ft(ac_lat, ac_lon, listener_lat, listener_lon)
    slant_ft = math.hypot(dist_ft, agl)
    lat_atten = 3 * math.log2(slant_ft / 500) if slant_ft > 500 else 0

    return max(0.0, base - vert - lat_atten)
```

### `TYPE_BASE_DBA` (1000-ft-AGL, 500-ft-slant baseline)

| Type | Base dBA | Note |
| --- | --- | --- |
| PA25 (Pawnee tow) | 85 | Ag engine, loudest single-engine GA |
| C152, C172, P28A | 75 | Standard trainer fleet |
| C182, P32R, SR22 | 78 | Higher-HP single |
| PA44, BE76 | 80 | ME piston trainer |
| PC12, TBM9 | 82 | Single-engine turboprop |
| C525, C25A, PC24 | 88 | Light jet |
| BE40, GLF4 | 92 | Mid/large jet |
| PW6, ASK21, DG500, K21, LS4 | 0 | Pure glider — engineless |

Look up the ICAO type code from
[noise/web/data/fleet.json](web/data/fleet.json) (per-tail) or the
embedded `TYPE_DESC` table at the top of
[noise/web/vite.config.js](web/vite.config.js) for descriptions. If a
type isn't in either, default base = 72 dBA and note the assumption.

`is_engineless_type` lives in
[noise/web/src/geo.js:isEnginelessType](web/src/geo.js). Mirror its
list: `['PW6', 'ASK2', 'ASK21', 'DG10', 'DG50', 'DG15', 'DG80',
'DG1T', 'DISC', 'VENT', 'NIMB', 'JS1J', 'ASTR', 'K21', 'LS4', 'SZD',
'PIK', 'STD', 'CLUB', 'LAK', 'GLID']`.

---

## Source attribution

For each flight, resolve "why was this aircraft here?" using these
fields, in priority order:

1. **Operator** from
   [noise/web/data/fleet.json](web/data/fleet.json) — most
   authoritative. Format `{ tail, type, operator, role }`.
2. **School** from
   [noise/web/data/flight_schools_fleets.json](web/data/flight_schools_fleets.json)
   — flight-school catalog with tail rosters.
3. **`base_airport`** (DB-derived) — where the aircraft typically
   lives. Surfaces on `/api/flights/current` as `base`.
4. **Aircraft category** — derived from ICAO type:
   - Trainer SE piston (C172, PA28, P28A) → flight school traffic
   - Tow plane (PA25) → glider club ops
   - Glider (PW6, ASK21, DG500) → glider club ops
   - Light jet (C525, PC24) → commercial / corporate
   - Multi-engine piston (PA44, BE76) → advanced training
   - SR22 / TBM → owner-operator GA

**Phase → purpose mapping:**

| Phase | Purpose |
| --- | --- |
| `pattern` | Training pattern work (touch-and-goes) |
| `inbound` | Inbound to land at this airport |
| `departing` | Climb-out from this airport |
| `practice_area` | Airwork (maneuvers, stalls, slow flight) |
| `en_route` | Transit (just passing through) |
| `nearby` | Transient — not based here, not landing here |
| `taxiing` | Ground operations |
| `on_ground` | Parked |
| `landed_full_stop` | Just completed a full-stop landing |
| `ack_pending` | Landed; acknowledge state |
| `climbing_on_tow` | Glider on aerotow (tow plane is the noise source) |
| `descending` | Soaring descent (engineless if glider, low power if powered) |

---

## File references for deeper reasoning

If the API doesn't give you what you need, these files contain the
canonical logic — read them rather than reinvent:

| File | What's in it |
| --- | --- |
| [noise/web/flightScore.js](web/flightScore.js) | Good Neighbor Score — combines aircraft type + altitude + population. Reference for the impact pipeline. |
| [noise/web/src/geo.js](web/src/geo.js) | `classifyPoint`, `distFt`, `trackLengthFt`, `isEnginelessType` |
| [noise/web/src/popGrid.js](web/src/popGrid.js) | `pointImpact`, `impactSegments`, `POP_KERNEL` (lateral attenuation kernel) |
| [noise/noise_heatmap.py](noise_heatmap.py) | Python implementation of the ground-noise kernel — mirrors flightScore.js |
| [noise/build_population_density.py](build_population_density.py) | Population density grid build |
| [noise/web/data/fleet.json](web/data/fleet.json) | ICAO hex → tail / type / operator / role |
| [noise/web/data/flight_schools_fleets.json](web/data/flight_schools_fleets.json) | Flight school catalog with tail rosters |
| [noise/web/data/runways.json](web/data/runways.json) | Curated runway data (FAA Form 5010 via AirNav) — has `elev_ft` |
| [noise/web/adsb.js](web/adsb.js) | `extractTowCycles`, `detectPhases`, `pairTowWithGliders` |
| [CLAUDE.md](../CLAUDE.md) | API contract — read it for current wire shapes |

---

## Domain cheat sheet

- **AGL = `alt_ft_msl − field_elevation_ft`** for the nearest
  airport. For listener-relative AGL, subtract the listener's terrain
  elevation.
- **Pattern envelope:** within `pattern_radius_nm` (KBDU=2 nm) AND
  ≤ 1500 ft AGL. Pattern flights live inside this envelope.
- **VNAP ceiling** = 7500 ft MSL default; some zones override (see
  `/api/noise-zones`).
- **Front Range airports** within 50 nm of KBDU: KBDU, KBJC, KAPA,
  KFNL, KEIK, KLMO, KGXY, KDEN.
- **Fleet attribution priority**: fleet.json → flight_schools.json →
  `base_airport` → aircraft-type heuristic.
- **Engine-type matters**: gliders make ~zero noise on their own; an
  aerotow generates noise from the *tow plane*, not the glider. Use
  `isEnginelessType` to zero-out gliders' contribution.
- **Server timestamps are UTC**. Convert to America/Denver for
  human-readable bins (UTC−7 standard, UTC−6 daylight).

---

## Worked example

Listener at **40.005, -105.205** (Frasier Meadows residential, ~2 nm
SE of KBDU, in the published noise-abatement polygon), window =
last 24 h, RADIUS_NM = 5.

1. `GET /api/flights/current?airport=KBDU&landed_hours=24&range_nm=25`
   → ~60 flights touched KBDU airspace in the window.
2. Also query each Front Range neighbor (KBJC, KAPA, KFNL, KEIK,
   KLMO, KGXY) with the same params — listener is close enough to
   KBDU that KBJC transits are audible.
3. Union the per-flight rows by `(tail, takeoff_ts)`.
4. For each unique flight, `GET /api/adsb/track/{icao}?since={takeoff_ts}`
   → `points[]`.
5. For each point in each track, compute slant range to
   (40.005, -105.205). Track the minimum slant range + the
   `(lat, lon, alt, ts)` at that point.
6. Filter to flights with closest-approach slant range ≤ 5 nm.
7. For each retained flight, plug into `est_dba_at_listener` at the
   closest-approach point.
8. Aggregate by hour, by type, by operator, by airport.
9. Emit the report sections in order.

---

## Stop-the-line conditions

- **No `/api/flights/current` results** for any airport — the noise
  picture is empty; emit the executive summary with `total=0` and
  stop. Don't synthesize.
- **All retained flights have base_dBA=0 (gliders)** — possible at
  KBDU on a summer Sunday. Note this in the methodology and report
  ambient = 0.
- **Track endpoint 404 for a tail** — note in the per-flight row
  (`alt_agl_ft = unknown`) and use the `/api/flights/current` summary
  fields for that row instead of skipping it.

---

## Don't bother with

- Real-time hooks (SSE / WebSocket). Use REST.
- Audio attachments — that's reporter audio, not propagated dBA.
- Pretty charts; tables of numbers are enough.
- Speculation about complaint impact — that's a separate analysis.
  This report is about **what arrived at the listener**, not what
  was complained about.

---

## Run command

The agent's caller will pass `LAT`, `LON`, `WINDOW`, `RADIUS_NM`,
`ELEV_FT_MSL`. Default to "last 24 h" if no window. Save the report
file, then print to chat:

```
Wrote noise/reports/noise_report_<LAT>_<LON>_<ISODATE>.md
Executive summary:
<one paragraph + key numbers>
```
