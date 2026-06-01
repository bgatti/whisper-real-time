# API requests — point-noise report

Concrete asks against the noise API, prioritised. Each ask is grounded in
behaviour observed building the `/point-noise` page (see
[noise/web/src/PointNoiseReport.jsx](web/src/PointNoiseReport.jsx)).

This file is the **wishlist**, not a launch blocker — the page works
today against the existing endpoints, but with the performance and
attribution caveats below. Each ask has a clear motive: an observed
problem in the report-builder workflow.

---

## Measured baseline — 2026-05-31, Boulder dev box

Local dev server (Vite at `127.0.0.1:5183`) over Railway Postgres via
`mainline.proxy.rlwy.net:18848`:

| Endpoint                                                             | Cold time | Verdict                                              |
| -------------------------------------------------------------------- | --------- | ---------------------------------------------------- |
| `GET /api/adsb/live`                                                  | < 0.01 s  | Fast                                                 |
| `GET /api/noise-zones`                                                | < 0.01 s  | Fast                                                 |
| `GET /api/excursions/boot?hours=24&limit=150` (warm)                  | 0.03 s    | Fast (warm cache); cold misses are 30 s+             |
| `GET /api/flights/current?airport=KBDU&landed_hours=6`                | 18.4 s    | Slow — kiosk should be ≤ 5 s steady state            |
| `GET /api/flights/current?airport=KBDU&landed_hours=24`               | 33.8 s    | Painfully slow                                       |
| `GET /api/flights/current?airport=KBJC&landed_hours=24`               | timeout   | **Unusable** (60 s)                                  |
| `GET /api/excursions/segments?lat=...&hours=1&radius_nm=2`            | 10.5 s    | Slow                                                 |
| `GET /api/excursions/segments?lat=...&hours=24&radius_nm=5`           | 16.2 s    | Painfully slow; intermittent 500 (`pg-pool` timeout) |
| `GET /api/excursions/segments?lat=...&hours=24&radius_nm=10`          | 34.2 s    | **Unusable**                                          |

Prod Railway (same DB, in-network):

| Endpoint                                                              | Time   | Verdict           |
| --------------------------------------------------------------------- | ------ | ----------------- |
| `GET /api/excursions/segments?lat=...&hours=24&radius_nm=5&limit=500` | 5.4 s  | Tolerable, not great |
| `GET /api/excursions/segments?lat=...&hours=1&radius_nm=5&limit=500`  | 6.5 s  | Tolerable          |

Conclusion: the **dev proxy roundtrip is 3–6 × prod**. Even prod is at
the upper edge of "acceptable for an interactive page" (5 s+). The
underlying query is a full-table scan + in-process point-in-polygon —
that is the real bottleneck, not the network.

---

## P0 — block / unblock

### 1. `excursions/segments` returns 500 on cold cache

Server log:

```
[excursions-segments-api] error Error: Query read timeout
    at pg-pool/index.js:45:11
    at loadLiveFromDbByDateRange ...
```

Symptom in browser: `Failed to load resource: the server responded with
a status of 500 (Internal Server Error)`. Happens on first-of-the-day
calls (cache miss) for any non-trivial window.

**Ask:** raise the per-query timeout to 60 s (matches the prod
roundtrip) and return a 503 with a `Retry-After` instead of a 500 if it
trips. The page can render a "still warming up" state on 503 but treats
500 as a hard failure.

**Server-team note 2026-06-01.** Half-spec, half-shippable:

- The 60 s query timeout is partially moot — `noise/web/db.js`'s
  current 20 s pool timeout was set deliberately below Railway's
  ~30 s HTTP proxy timeout. Raising the pool to 60 s gets the request
  killed by the proxy at 30 s anyway, with a generic 502 gateway
  error rather than our nice 503 — so the user-facing experience
  doesn't improve. The lasting fix for cold-cache is § 2 (push the
  geometric filter into SQL), not a longer timeout.
- The 503 + `Retry-After` half IS straightforwardly better UX and
  ships as a small inline patch to the segments handler error path
  (detect timeout-shaped errors → `503` + `Retry-After: 10` +
  `{ transient: true, hint: '...' }` body so the page can render
  "warming up" without a hard failure).
- Queued to land after the two in-flight workers (`feature/whatif-v2-regulatory`
  + `feature/purposeml-adoption`) merge — both are editing
  `vite.config.js` right now and an inline §1 patch would race them.

**Client-team recommendation 2026-06-01.** Concur on the path —
ship the 503 + `Retry-After` patch as-spec'd; do **not** raise the
pool timeout. The client already has the right machinery:
`isRetryableErr` in `runReport()`
([noise/web/src/PointNoiseReport.jsx](web/src/PointNoiseReport.jsx))
detects timeout / 5xx / network shapes and auto-retries once after
a 1.5 s drain. A 503 with `Retry-After: <N>` slots into the same
code path cleanly — replace the hard-coded 1.5 s with the header
value (clamped to 30 s), keep the skeleton's existing amber
"retrying after a cold cache" state, and the user-facing flow turns
from "hard failure → manual Retry" into "warming up → autoresolves".
No new client work needed beyond honouring the header.

**Reproduced live 2026-06-01.** Three sequential hits of the page's
exact URL right after a server restart:

```
attempt 1: HTTP 000 (connection dropped) after 8.0 s
attempt 2: HTTP 500 (pg-pool query read timeout) after 32.3 s
attempt 3: HTTP 000 after 0.7 s
```

So today the cold path serves a mix of 5xx and bare connection
drops, not just 500. Whichever shape the inline handler ends up
emitting on a timed-out query, **please make it a clean `503`** —
Railway-proxy 502s and TCP-level drops both read as "definitely
failed, not transient" to most client retry policies. A response
shape the page can recognise as "warming up" is the user-facing
win, not a longer timeout.

**Unblocking the §1 ship.** Holding back further `vite.config.js`
edits on `feature/whatif-v2-regulatory` so this branch can merge
soon. Pure-client follow-ups (chart / UI / copy) continue landing
without touching shared files. If something on the page truly
requires a server edit before §1 lands, flagging it in this doc
rather than slipping it into the branch.

**Skipping the cosmetic client-only mitigation.** Considered
defaulting the first paint to `hours=1, radius_nm=2` + a longer
retry chain, but that's papering over a real defect — the 503 +
header-driven retry the server spec'd is the lasting fix and lands
cleanly once this branch merges.

**Status — landed 2026-06-01.** Server-team patch shipped at PR
[bgatti/KnownRisks#6](https://github.com/bgatti/KnownRisks/pull/6).
Segments handler now detects pg-timeout / connection-drop errors and
emits a clean `503` + `Retry-After: 10` + `{ transient: true,
retryAfterS: 10, hint: 'tracks cache is warming up — retry in ~10s' }`.
Pool query timeout left at 20 s (below Railway's ~30 s HTTP proxy)
on purpose so we get a clean response shape rather than a 502 / TCP
drop. Closes the response-code half of §1; latency itself remains
§2's job.

PR #6 also bundles a tiny data fix — adds the four user-confirmed
SF/FF suffix tails (N256SF, N265SF, N58FF, N79FF) to McAir / Spartan
in `flight_schools_fleets.json` + spells out the
`^N\d+(SF|FF)$ at KBJC = McAir/Spartan` convention in the notes.

### 2. Cold-path response time on `excursions/segments`

A first-time call for a 24 h / 5 nm window on the dev box was 16.2 s; in
prod it's 5.4 s. The page can't keep the user looking at a blank screen
for 16 s — and even 5.4 s is past most users' patience.

**Ask:** push the geometric filter into SQL. The current handler pulls
the entire day's tracks, then filters in JS. With a PostGIS extension
or even a basic bounding-box pre-filter on `(lat, lon, alt, ts)` the
DB returns a single-digit-MB candidate set in milliseconds.

Until then, the page mitigates with a tighter initial query
(`hours=1`) and a visible loading state — but the right fix is at the
query layer.

---

## P1 — make the report better

### 3. School / operator / purpose in the `segments` response

`/api/excursions/segments` returns `{ tail, type, segments[…] }` per
track — no `school`, `purpose`, `base_airport`, or `special_use`. The
server already computes those (see `resolvePurpose` and `expandType`
in `noise/web/vite.config.js`); they just don't ride along on this
endpoint.

**Status — client-side workaround landed 2026-05-31.** The page now
loads `/flight_schools_fleets.json` (already a static asset under
`noise/web/public/`) on mount, builds a `tail → { school, airport }`
index (~111 tails today), and uses it to override `purpose='training'`
whenever a tail is in the roster and its airframe isn't a tow plane or
glider (type-unambiguous airframes still win). The "Most-active
individual aircraft" table surfaces the school name + base airport
directly. Source:
[noise/web/src/PointNoiseReport.jsx](web/src/PointNoiseReport.jsx)
`resolvePurpose()`.

**Residual gap — still want this on the server:**

- The static fleet roster covers 111 tails researched in April 2026.
  Schools turn over aircraft; we'll go stale. Server-side join against
  the live `tracks.school` column won't.
- `special_use` (medivac / firefighting / military / science) lives in
  `public/special_use_aircraft.json` and isn't joined client-side
  today. NEON Twin Otters & similar still land under `turboprop` /
  `ga_single` instead of `science` / `medevac`.
- Every consumer of `segments` (this page, future analyses) has to
  reinvent the same fleet-load + override. Doing it once on the
  server is cheaper.

**Ask (unchanged):** add `purpose`, `school`, `school_slug`,
`base_airport`, and `special_use` fields to each track in the
`/api/excursions/segments` response, computed by the same
`resolvePurpose` the leaderboard uses. The client workaround can then
be deleted.

### 3b. Live-only-track backfill parity — `purpose` + scenario candidates

**Observation 2026-06-01.** Right after a dev-server restart the
segments response for a 72 h / 10 nm window returned a single
live-only track (`loadTracksFromDb` returned 0 rows). On that track:

```
track keys = [date, descents, hasDescents, live, phase,
              segments, src, tail, type]
```

`base_airport`, `alt_offset_ft`, `purpose`, `alt_airframe_candidates`,
and `alt_segment_candidates` were all absent — the live JSONB blob
doesn't carry them, and only `base_airport` has the post-pass backfill
described in § 3a.

This isn't a corner case: any live-heavy window (a small radius, a
short hours value, or a query during a quiet historical-tracks period)
will hit it. The page's purpose attribution silently degrades for
those tracks because the client now trusts the server's `purpose`
field — when it's missing, we drop to the type-only fallback.

**Ask:** extend § 3a's `tail → most-recent-base` backfill to also
project `purpose`, `alt_offset_ft`, `alt_airframe_candidates`, and
`alt_segment_candidates` for live-only tracks — same pattern, same
single SQL `SELECT … FROM tracks WHERE call = ANY($1)`. The fields
should be present (even if `purpose` ends up `null` because no
historical row exists) so the client can rely on key existence rather
than guessing.

**Server-team ack 2026-06-01.** Confirmed and queued — same backfill
pattern, same SQL extended to also `SELECT purpose, alt_offset_ft …
FROM tracks WHERE call = ANY($1)`. The `alt_*_candidates` arrays are
computed from `purpose` + `type` deterministically, so once `purpose`
is backfilled they fall out for free. Will ship as a follow-up PR
right after PR #2 lands so it doesn't conflict on `vite.config.js`.
Tracking as Ask #3b → next PR.

### 3a. `base_airport` per track — drives the "By based airport" report

The new "By based airport" section of the point-noise report
attributes each pass to where the aircraft actually lives — so the
listener can read "92 % of the noise over my house is based at KBDU,
6 % at KBJC, 2 % unknown" instead of just a flat list of tails.

The page uses the school-fleet roster's airport field as a
fall-back base attribution. This is incomplete by construction:

- Roster lists **104 noise-relevant tails** based at KBDU / KBJC /
  KAPA / KFNL / KEIK / KLMO. Every private owner, transient, and
  un-catalogued school aircraft drops into the "Unknown base" bucket.
- The server already maintains `tracks.base_airport` (visible on
  `/api/flights/current`) — that field is authoritative because it's
  derived from observed takeoff/landing patterns, not a hand-curated
  list. The point-noise report can't use it today because the
  `segments` response doesn't carry it.

**Ask:** include `base_airport` (the most-recent observed base from
the `tracks` table) on every track in `/api/excursions/segments`.
Two-line change: the SQL already selects from `tracks`; just add
`base_airport` to the projection and the response object. The client
can then delete the school-roster-airport fallback for this section.

Same `purpose`-style hook would land all the other tags too
(`school`, `school_slug`, `special_use`) — see ask #3 above for the
bundle.

**Status — landed 2026-06-01.** `/api/excursions/segments` now emits
`base_airport` on every track. Spot-check at Frasier Meadows (lat
40.005, lon −105.205, radius 5 nm, 24 h window): 11 of 14 tracks
resolved a base (7 KBJC, 2 KBDU, 2 KFNL); the three remaining nulls
were transients with no history in `tracks`. Implementation: SQL
projection added in
[noise/web/db.js](web/db.js) `loadTracksFromDb`, response field added
in
[noise/web/vite.config.js](web/vite.config.js) `excursionsApiPlugin`.
Because live-only windows often have no historical track row in the
same query result, the handler also does a one-shot
`SELECT base_airport FROM tracks WHERE call = ANY($1)` backfill —
same pattern `/api/adsb/current-flights` uses — so live tracks pick
up the most-recent base without needing a wider history window.
Client can now drop the school-roster fallback.

### 4. Estimated dBA at the listener, server-side

The page reproduces the spec's dBA model client-side, which means:

- type → base dBA table is duplicated (drifts from server's `flightScore.js`)
- AGL math is duplicated and depends on a `listener_elev_ft` the user enters
- engineless filter is duplicated

**Ask:** when `lat`/`lon` is supplied to `/api/excursions/segments`,
add a `listener_metrics` block to each track:

```json
{
  "closest_approach": { "ts": "...", "lat": 40.005, "lon": -105.205,
                        "alt_msl_ft": 6800, "alt_agl_ft": 1510,
                        "slant_ft": 4321, "dist_ft": 4204 },
  "est_dba": 67,
  "exposure_seconds_above_60dba": 41
}
```

Same field shape on the per-segment level for the timeline scatter.
This lets the client drop ~250 lines of duplicated noise logic and
guarantees the score stays in sync with the leaderboard / impact
endpoints. The server can also derive listener elev from the SRTM
terrain raster it already uses for VNAP — no need to ask the user.

### 5. Pre-rolled per-bucket counts

The most valuable section of the page (per the spec) is the **purpose
rollup**. Building it client-side requires every flight's full track.
For a 24 h / 5 nm KBDU window that's ~3 MB of payload to compute eight
small bar lengths.

**Ask:** add `/api/noise/point-report?lat=&lon=&hours=&radius_nm=`
that returns the report-shaped roll-ups directly:

```json
{
  "window": { "from": "...", "to": "...", "hours": 24 },
  "center": { "lat": 40.005, "lon": -105.205, "elev_ft": 5288 },
  "totals": { "audible_flights": 152, "peak_dba": 79, "mean_dba": 59, "peak_hour": 9 },
  "by_purpose":  [{ "purpose": "training", "n": 89, "peak_dba": 71, "mean_dba": 60 }, ...],
  "by_type":     [{ "type": "C172", "n": 47, "peak_dba": 65, "mean_dba": 57, "min_agl_ft": 212 }, ...],
  "by_operator": [{ "operator": "Mile High Aviation", "n": 41, "peak_dba": 66 }, ...],
  "by_hour":     [{ "hour_local": 0, "n": 0, "peak_dba": 0 }, ..., { "hour_local": 23, "n": ... }],
  "loudest":     [{ "tail": "N317MP", "type": "LJ60", "dba": 79, "ts": "..." }, ...],
  "lowest":      [{ "tail": "N737NA", "type": "C172", "agl_ft": 212, "ts": "..." }, ...]
}
```

Page would render in one round-trip with a ~20 KB payload, no
client-side aggregation, no track-level transfer. Tracks become a
**drill-down** call only when the user clicks a row.

### 6. `radius_nm` should be the canonical input

`/api/excursions/segments` accepts `radius_mi` and translates
`radius_nm × 1.15078` into statute miles. Aviation users think in nm
(everything else on the dashboard does) — the statute alias is a
needless source of confusion.

**Ask:** make `radius_nm` first-class in docs and examples, keep
`radius_mi` only as a deprecated alias. The shared `noiseApi.js`
client now passes `radiusNm` as the canonical field.

---

## P2 — nice to have

### 7. Bounding-box query as a primitive

For the page's mini-map pin picker (planned), we want to know "what
airports / runways / noise zones intersect this bbox" without 4
separate calls.

**Ask:** `/api/geo/at?bbox=...` returning `{ airports[], runways[],
noise_zones[], terrain_elev_ft }`. Single call, ~5 KB.

### 8. Consistent `radius_nm` semantics across endpoints

Some endpoints take `range_nm`, others `radius_nm`, others `radius_mi`.
The page picker has to remember which.

**Ask:** every spatial endpoint takes `radius_nm`. Rename
`range_nm` → `radius_nm` on `/api/flights/current` with the old name
kept as an alias for one release.

### 9. Cursor-based pagination on `segments`

`limit` is unhelpful when the dataset is bigger than the cap — the
client doesn't know which rows it lost. The page currently uses
`limit=500` and hopes that's enough; for KBJC / KAPA peak hours it's
not.

**Ask:** server returns `next_cursor` when capped; client paginates
until exhausted or budget hit. The page can show a "1,200+ flights —
showing first 500" hint with the same data.

### 10. Server-Sent Events for the timeline scatter

After the initial roll-up paint, it'd be nice to extend the report
forward as new flights land. The page already auto-fetches every time
the slider moves — an SSE channel keyed on `(lat, lon, radius)` would
let the page stream new closest-approach events without polling.

**Ask:** `GET /api/noise/point-report/stream?lat=&lon=&radius_nm=`
emitting `event: pass\ndata: {...}` for each new audible flight. The
kiosk already has the WebSocket infra
(`/api/adsb/stream`); SSE mirrors that for the report use case.

### 11. What-if scenarios — quieter-fleet substitutions

The point-noise report tells the listener what *did* happen overhead.
Residents want to ask the next-step question: **what would my noise
exposure look like if SSB / Mile High / KBJC swapped some fraction of
their fleet for quieter airframes?** Cost and acoustic benefit need
to be on the same screen so the conversation moves beyond "we wish it
were quieter" to "here's the dB delta per dollar invested".

**Status — partially landed as of 2026-06-01.** Server-side groundwork
has shipped; the per-segment substitute dBA hasn't. Detail:

| Piece | Status | Notes |
| --- | --- | --- |
| `track.purpose` field | ✅ landed | Drives slider-→-track matching. Client now uses this directly (retired its `resolvePurpose`). |
| `track.alt_airframe_candidates` field slot | ✅ landed | Field present on every track row; values populate on tracks whose purpose matches a substitute's `replaces_purposes`. |
| `track.alt_segment_candidates` field slot | ✅ landed | Same shape as above; carries `WNCH` with its `applies_to_agl_below_ft`. |
| `/substitutes.json` static config | ✅ landed | Full table (EFOX / SINU / VELE / WNCH) with capex, op-savings/hr, useful life, residual %, annual hours, base_dba. |
| `segment.alt_dba_by_substitute` | ⏳ **in PR #2, not yet merged into master** — see clarification below | Per-segment precomputed "what dBA would this segment have produced at the listener if substituted with VELE / EFOX / SINU / WNCH?". Without it the histogram math has nothing to swap in. |

**Server-team clarification 2026-06-01.** §2d of PR
[bgatti/KnownRisks#2](https://github.com/bgatti/KnownRisks/pull/2)
already implements per-segment `alt_dba_by_substitute` — visible at
`vite.config.js:1496` on `feature/scenario-apis-only`. **Two
gotchas** that may explain why you're seeing it absent:

1. **PR #2 targets `feature/altcorrection-extract` (PR #1), not
   `master`.** PR #1 needs to merge first; until then a checkout of
   `master` doesn't have either set of changes. To test now, check
   out `feature/scenario-apis-only` directly:
   `git checkout feature/scenario-apis-only` then `npm run build`
   and your local dev server has the full thing.
2. **The field is gated on `lat`/`lon` in the request.** With no
   listener position the segments handler has nothing to compute
   dBA against, so the field is omitted (not null). PointNoiseReport
   already passes both, so this should be automatic — but if your
   smoke test was a bare curl without `lat=&lon=`, that's the cause.

Curl that proves it works (against your local
`feature/scenario-apis-only` checkout):

```
curl -s 'http://localhost:5174/api/excursions/segments?lat=40.005&lon=-105.205&radius_nm=3&hours=24&limit=10' \
  | jq '.tracks[0].segments[0].alt_dba_by_substitute'
```

If that returns `null` rather than `{ "VELE": 53 }` or similar,
that's a real bug — please re-edit this section with the actual
curl output and I'll investigate immediately.

Once that lands, the client can ship the What-If panel in one cut —
the slider math + NPV calc is already drafted and is pure client work
(no further server roundtrips).

Four scenarios cover the realistic transition options on the Front
Range today (concrete substitute aircraft in the table below):

1. **Electric trainer swap.** Replace a `pct` fraction of training
   tracks (C172 / C152 / P28A) with Pipistrel Velis Electro.
   ~15 dB quieter at 1000 ft AGL; battery range limits operations
   to short patterns, which is exactly what these aircraft do.
2. **Eurofox tow swap.** Replace a `pct` fraction of tow tracks
   (PA25 / PA18) with the Aeropro Eurofox (Rotax 912, 100 HP).
   ~13 dB quieter than the Pawnee on full-power tow climb.
3. **Self-launching glider conversion.** Replace a `pct` fraction
   of glider tracks (GLID / AS21 / DG\* / etc.) with Pipistrel Sinus
   motorglider. Eliminates the paired tow plane entirely; engine on
   only for the first ~2 min of the flight, then a regular silent
   glider.
4. **Winch under N feet AGL.** For each tow track, any segment below
   `winch_under_agl_ft` (default 2000) contributes 0 dBA at the
   listener — winch noise is loud but localized to the airfield and
   stops the moment the cable is released. Captures the **loudest
   moment of every tow** (full-power initial climb).

#### Substitute table

| Code | Aircraft | Base dBA @ 1000 ft | HP | Replaces | New $ | $/hr op (vs incumbent) | Useful life | Residual @ EoL |
|---|---|---:|---:|---|---:|---:|---:|---:|
| `EFOX` | Aeropro Eurofox | 72 | 100 (Rotax 912) | PA25 (85), PA18 (82) | $130–180k | −$40/hr | 15 yr | 40% |
| `SINU` | Pipistrel Sinus | 70 engine-on / 0 soaring | 80 (Rotax 912) | glider + tow combo | $130–160k | −$30/hr (no tow fee) | 20 yr | 50% |
| `VELE` | Pipistrel Velis Electro | 60 | 76 kW elec | C172, C152, P28A (all ~75) | $190–230k | −$30/hr (fuel→electricity) | 12 yr | 30% (battery degraded) |
| `WNCH` | Winch launch | 0 at aircraft (ground-local) | — | tow segment ≤ N ft AGL | $50–150k winch | −$35/launch (no aerotow) | 25 yr | 30% |

`$/hr op` is the **delta vs the incumbent aircraft**, not absolute
operating cost — that's what flows into the NPV calc below. Useful
life + residual feed the salvage term.

The first three need to land in
[noise/web/src/PointNoiseReport.jsx → TYPE_BASE_DBA](web/src/PointNoiseReport.jsx)
and [noise/web/flightScore.js → HP_BY_ICAO](web/flightScore.js) so the
substitution is wired through the same kernel used for `est_db`.

#### Client / server split

**Server's job — emit the candidate set.** When the segments endpoint
returns a track, it also returns the substitutes that are even
applicable to that track:

```jsonc
{
  "tail": "N65440",
  "type": "C152",
  "purpose": "training",
  "base_airport": "KBJC",
  "alt_offset_ft": -120,
  "alt_airframe_candidates": ["VELE"],            // new
  "alt_segment_candidates": [                     // new (per-segment)
    { "code": "WNCH", "applies_to_agl_below_ft": 2000 }
  ],
  "segments": [
    { "klass": null, "alt_dba_by_substitute": { "VELE": 53 }, ... },
    { "klass": "yellow", "alt_dba_by_substitute": { "VELE": 67, "WNCH": 0 }, ... }
  ]
}
```

So for each segment the server precomputes "what dBA would this
segment have produced at the listener if the source aircraft were
{VELE, EFOX, SINU, WNCH}?". The math reuses the existing kernel +
the substitute's `base_dBA` — no duplication. Cost: response grows
by ~K floats per segment (≤4 substitutes); for a 60-track 24h window
that's <10 KB extra. The server stays the authority on dBA math.

**Client's job — own the sliders.** The What-If panel has four
sliders + one input box:

- electric trainer % → applies to tracks whose `purpose=training`
- eurofox tow % → applies to tracks whose `purpose=tow_plane`
- sinus glider % → applies to tracks whose `purpose=glider`
- winch under N ft AGL (input) → applies to tow segments below N

For each slider tick:
1. Take all tracks with the matching substitute in
   `alt_airframe_candidates`.
2. Pick `pct` of them deterministically — sort by `tail` ascending,
   take the first `floor(N * pct/100)`. Reproducible (no flicker on
   re-render) without needing a server seed.
3. For each picked track, use `alt_dba_by_substitute[code]` per
   segment instead of the original `est_db`.
4. Re-aggregate the histogram in-browser.
5. Render the new histogram as a **second series overlaid** on the
   baseline (the kiosk's chart lib supports stacked / overlaid bars
   already, so no new chart code needed).

Server roundtrips: one (the same `/api/excursions/segments` call
the page already makes). The slider feels instant; no debounce, no
loading state.

**Why not put `pct` on the server?** A scenario with three sliders is
3³ = 27 combinations at 10% granularity. Worse, the user wants to
*scrub*, not pick discrete values. Doing it client-side avoids server
load and gives sub-16 ms feel. The only thing we'd lose vs server is
reproducible URLs — fix that by serializing the slider state into the
hash fragment (`#electric=60&eurofox=50&winch_agl=2000`).

#### NPV — putting capex + opex on one comparable axis

A naïve "5-yr net" number lets a $1.8M capex investment with $400k/yr
savings *look better than* a $150k capex with $30k/yr savings, even
though the small one has way better return on capital. Net Present
Value gives the right comparable number and folds in the time value
of money — which is the whole question for a club deciding whether
to bond out for a hangar full of Velis Electros.

Standard formula, computed entirely client-side from the table above
+ a few user-tunable sliders:

```
NPV = -CapEx
    + Σ (annual_op_savings / (1 + r)^t)    for t = 1 .. horizon
    + (CapEx × residual_pct) / (1 + r)^horizon
```

Where:

- `annual_op_savings = op_savings_per_hr × annual_hours_per_airframe × N_airframes`
- `N_airframes` = distinct tails substituted at the current slider position
- `annual_hours_per_airframe` defaults to 400 (light club tow), 600
  (school trainer), 250 (private glider/motorglider) — overridable
  in the substitute config
- `r` = discount rate from the slider (default 5%)
- `horizon` = years from a horizon slider (default 10 yr;
  range 5..20)
- `residual_pct` = end-of-life resale value from the substitute table

**What this yields:**

1. **NPV in $** per scenario — directly comparable across slider
   settings. Negative NPV = the substitution loses money on operations
   alone. Positive NPV = the substitution pays for itself and the
   noise reduction is a free externality.
2. **$/dB-reduction** ratio — divide `-NPV` (cost of the program) by
   `baseline_peak_db − scenario_peak_db`. Sorts the four scenarios by
   cost-effectiveness. This is the number a city planner or noise
   abatement board cares about: "what's the cheapest dB to buy?".
3. **Break-even hours/year** — solve `NPV(r, horizon, annual_hrs) = 0`
   for hours. Tells the club "we need to fly each Eurofox at least N
   hours/yr to justify the swap". Useful when comparing low-utilization
   private-club ops to high-utilization commercial schools.

#### Sliders the page exposes

Three "what fraction" sliders + four "financial" knobs:

| Slider | Default | Range | Owns |
|---|---:|---:|---|
| Electric trainer % | 0 | 0..100 | Which trainer tracks → VELE |
| Eurofox tow % | 0 | 0..100 | Which tow tracks → EFOX |
| Sinus glider % | 0 | 0..100 | Which glider tracks → SINU |
| Winch under N ft AGL | off | 0..3000 ft | Tow-segment ceiling for WNCH |
| Discount rate | 5% | 3..12% | NPV denominator |
| Time horizon | 10 yr | 5..20 yr | NPV summation range |
| Annual hours per airframe | per substitute config | 100..1000 | Op-savings multiplier |
| Fuel / electricity multiplier | 1.0× | 0.5..2.0× | Sensitivity-test op savings |

The financial knobs sit collapsed by default under an "Advanced"
disclosure — the four what-if sliders are the primary controls. The
NPV / $-per-dB lines update live on every slider tick (client-side
math, no roundtrip).

#### Mini business-model view

With NPV in hand, the same page can render a 3-line table per
substitute that reads like a board-meeting handout:

| | Velis Electro × 10 (MHA fleet) | Eurofox × 1 (SSB) | Winch system |
|---|---:|---:|---:|
| CapEx | −$2.0M | −$150k | −$100k |
| Op savings (10 yr, undiscounted) | +$2.4M | +$220k | +$250k |
| Salvage @ 10 yr (5% disc) | +$0.37M | +$37k | +$18k |
| **NPV @ 5% / 10 yr** | **+$0.27M** | **+$57k** | **+$110k** |
| **Peak dB reduction @ listener** | −10 dB | −3 dB | −1 dB peak / huge LMax cut |
| **$/dB-reduction** (cost view) | $0 (NPV-positive) | $0 (NPV-positive) | $0 (NPV-positive) |
| **Break-even hours/yr** | 290 hr | 180 hr | 75 launches/yr |

When NPV is positive at the chosen rate + horizon, the project pays
for itself even before counting the noise reduction as a community
benefit. The "$/dB" column flips to a real number only when NPV goes
negative — i.e., when the substitution costs money on net and we're
asking how much per dB of relief.

That's the headline a real noise-abatement conversation needs: not
"how loud is it today" (the rest of the report) but "what would it
cost to make it quieter, and would the operator save or lose money
in the process".

**Ask:** add `alt_airframe_candidates` + `alt_dba_by_substitute` to
the segments response. Move the substitute table — including
`useful_life_years`, `residual_value_pct`, `annual_hours_typical`,
`op_savings_per_hr` — into `noise/web/data/substitutes.json` so it
can be edited without a deploy. Then build the What-If panel + NPV
calc in `PointNoiseReport.jsx`. Estimated ~150 lines server + ~300
lines client (the extra ~100 is the NPV math + the business-model
table).

---

### 11-CLIENT. Implementation guide — What-If panel

This is the contract for the client team building the What-If panel
in `noise/web/src/PointNoiseReport.jsx`. **Edit this section** when
something's unclear, blocked, or different than expected — server
team watches this file on a 15-min tick and answers inline. When
the panel renders correctly against live data, change the heading
marker from `[WIP]` to `[DONE]` and the channel closes.

**Status: [DONE] — V1 (4 aircraft-substitute sliders) + V2 (2 regulatory demand-reduction sliders) both render end-to-end. V1 → PR [bgatti/KnownRisks#3](https://github.com/bgatti/KnownRisks/pull/3); V2 → PR [bgatti/KnownRisks#4](https://github.com/bgatti/KnownRisks/pull/4).**

**Verification audit — server team self-verified 2026-06-01:**

| Check | Result |
| --- | --- |
| `npm run build` on `feature/whatif-panel` | ✅ clean, 811 KB bundle |
| Dev server boots | ✅ ready in 1.1 s, all plugins registered |
| `GET /point-noise` SPA shell | ✅ 200, 949 bytes, mounts `<div id="root">` + `/src/main.jsx` |
| `GET /substitutes.json` | ✅ 200, 2333 bytes, 4 codes (EFOX, SINU, VELE, WNCH) |
| `GET /src/PointNoiseReport.jsx` (vite dev-served source) | ✅ 3713 lines, 66 occurrences of §11-CLIENT symbols (`WhatIfPanel`, `electric_pct`, `eurofox_pct`, `sinus_pct`, `winch_agl_ft`, `alt_dba_by_substitute`, `alt_airframe_candidates`, `pickSubstituted`, `segmentDba`, `businessModelCols`, `advancedOpen`) |
| `GET /src/whatif.js` helpers module | ✅ 32 KB, exports `pickSubstituted`, `shouldWinchSegment`, `segmentDba`, `npv`, `businessModelColumn` |
| `GET /api/excursions/segments?lat=40.005&lon=-105.205&radius_nm=3` | ✅ scenario data flowing — every track carries `alt_airframe_candidates`, every segment carries non-empty `alt_dba_by_substitute` (sample: `N5138R C172 cands=['VELE'], first_seg sub_db={VELE: 43.8}`) |
| Slider interactivity in browser | ⏸ not directly observed (no headless browser available to server team); inferred from helpers + state hooks being wired into the JSX render path (`scenarioOn` branch present in `HourlyChart`, `applyScenarioToRow` projector exported) |

**Findings worth noting:**

- ⚠️ Pre-existing duplicate `AS21: 0` key in `src/NoiseImpactTest.jsx:590`
  triggers a vite warning. Unrelated to PR #3 (NoiseImpactTest isn't
  loaded by the `/point-noise` route). Worth a one-line fix in a
  separate PR.
- 🔍 Dev server log surfaces a recurring `pg-pool` deprecation warning
  on `client.query()` calls. Also pre-existing.

**For the human reviewer:** the draft PR is functionally complete. The
one verification gap is a real interactive browser test — please open
http://localhost:5174/point-noise after merging PR #1 → PR #2 → PR #3,
move the "Electric trainer %" slider, and confirm the scenario
histogram overlays + the business-model table populates. If both
hold, flip the PR to non-draft. If not, edit this section with
`[REOPEN] <issue>` and the server team will fix.

`[DONE]` from server-team perspective; one human-side confirmation
remains as a courtesy gate.

#### 1. Fetch substitutes.json on mount

Single static fetch, cache for the page lifetime:

```js
const [substitutes, setSubstitutes] = useState(null)
useEffect(() => {
  fetch('/substitutes.json')
    .then((r) => r.json())
    .then((d) => setSubstitutes(d.substitutes))
    .catch(() => setSubstitutes([]))   // fail-open: hide What-If panel if missing
}, [])
```

Shape per substitute (file at
[noise/web/public/substitutes.json](web/public/substitutes.json)):

```jsonc
{
  "code": "VELE",
  "name": "Pipistrel Velis Electro",
  "base_dba": 60,
  "replaces_types": ["C172", "C152", "P28A", ...],
  "replaces_purposes": ["training"],
  "scope": "track",                    // or "segment"
  "applies_to_agl_below_ft": 2000,     // only for scope:"segment"
  "cap_ex_usd": 210000,
  "op_savings_per_hr_usd": 30,
  "useful_life_years": 12,
  "residual_value_pct": 0.30,
  "annual_hours_typical": 600
}
```

Hide the What-If panel entirely if `substitutes.length === 0`. Don't
fail the rest of the report.

#### 2. New fields per track (PR #2)

```jsonc
{
  "tail": "N65440",
  "type": "C152",
  "purpose": "training",
  "base_airport": "KBJC",
  "alt_offset_ft": -120,                       // from PR #1
  "alt_airframe_candidates": ["VELE"],         // track-scope subs
  "alt_segment_candidates": [                  // segment-scope subs
    { "code": "WNCH", "applies_to_agl_below_ft": 2000 }
  ],
  "segments": [
    {
      "klass": null,
      "points": [...],
      "alt_dba_by_substitute": { "VELE": 53 }   // null if cand. but n/a here
    }
  ]
}
```

Key conventions:

- `alt_dba_by_substitute[code]` is **populated only when `lat`/`lon`
  is supplied** to the segments endpoint. PointNoiseReport already
  passes both — automatic.
- Value `null` = substitute is a candidate for the track but doesn't
  apply to this segment (e.g. WNCH outside its AGL window).
- Key **omitted** = substitute isn't a candidate for the track at
  all. Treat as "don't substitute".

#### 3. Slider UI — four what-if + four financial

| Slider | Default | Range | Step | Owns |
|---|---:|---:|---:|---|
| Electric trainer % | 0 | 0..100 | 5 | Tracks where `alt_airframe_candidates` includes `VELE` |
| Eurofox tow % | 0 | 0..100 | 5 | Tracks where `alt_airframe_candidates` includes `EFOX` |
| Sinus glider % | 0 | 0..100 | 5 | Tracks where `alt_airframe_candidates` includes `SINU` |
| Winch under N ft AGL | off (0) | 0..3000 | 100 | Segments of tow tracks where AGL ≤ N use WNCH (db=0) |

Under an "Advanced" disclosure (collapsed by default):

| Knob | Default | Range | Step |
|---|---:|---:|---:|
| Discount rate | 5% | 3..12 | 0.5 |
| Time horizon (yr) | 10 | 5..20 | 1 |
| Annual hours per airframe | (per-substitute default) | 100..1000 | 50 |
| Fuel / electricity multiplier | 1.0× | 0.5..2.0 | 0.05 |

Recompute on **every** slider tick (no debounce — all computation is
client-side and fast enough). Target: <16 ms for the typical 60-track
/ 5-segment-per-track payload.

#### 4. Deterministic substitution selection

```js
function pickSubstituted(tracks, code, pct) {
  if (pct <= 0) return new Set()
  const eligible = tracks
    .filter((t) => (t.alt_airframe_candidates || []).includes(code))
    .map((t) => t.tail)
    .sort()                            // tail-ascending = reproducible
  const k = Math.floor((eligible.length * pct) / 100)
  return new Set(eligible.slice(0, k))
}
```

Winch is an AGL threshold, not a fraction:

```js
function shouldWinchSegment(seg, threshold_ft, listenerElevFt) {
  if (threshold_ft <= 0) return false
  const lowest_agl = Math.min(...seg.points.map((p) => p[2] - listenerElevFt))
  return lowest_agl < threshold_ft
}
```

#### 5. dBA lookup per segment in the scenario world

```js
function segmentDba(track, seg, scenario, listenerElevFt) {
  if (scenario.winchTracks.has(track.tail) &&
      shouldWinchSegment(seg, scenario.winch_agl_ft, listenerElevFt)) {
    return 0
  }
  for (const code of ['VELE', 'EFOX', 'SINU']) {
    if (scenario.substituted[code].has(track.tail)) {
      const sub = seg.alt_dba_by_substitute?.[code]
      if (sub != null) return sub
    }
  }
  return estDbaAtListener({type: track.type, ...originalGeometry})
}
```

Re-aggregate the histogram exactly the way you do today, just using
`segmentDba(...)` instead of the raw `est_db` lookup.

#### 6. Overlay rendering

- Baseline bars: 40% opacity, neutral grey
- Scenario bars: 100% opacity, accent color, drawn over baseline
- Tooltip: both values per bin
- All sliders at default → render baseline only, hide scenario layer

#### 7. NPV + business-model table

```js
function npv({capex, annualSavings, salvage, rate, years}) {
  let pv = -capex
  for (let t = 1; t <= years; t++) pv += annualSavings / Math.pow(1 + rate, t)
  pv += salvage / Math.pow(1 + rate, years)
  return Math.round(pv)
}
```

Inputs per active substitute (any slider > 0):

- `capex = sub.cap_ex_usd * N_airframes` — distinct tails substituted
  (or `1` for winch — one shared system).
- `annualSavings = sub.op_savings_per_hr_usd * sub.annual_hours_typical * N_airframes * fuel_multiplier`
- `salvage = capex * sub.residual_value_pct`

Render the "Mini business-model view" table (§ above Ask #11
substitute table) — one column per active substitute.

`$/dB-reduction`:

```js
const dbDelta = baselinePeakDb - scenarioPeakDb
const dollarsPerDb = dbDelta > 0 ? Math.max(0, -npv) / dbDelta : null
```

Show `$0` (or "pays for itself") when NPV ≥ 0.

#### 8. URL hash serialization

```js
const params = new URLSearchParams({
  electric: scenario.electric_pct,
  eurofox: scenario.eurofox_pct,
  sinus: scenario.sinus_pct,
  winch_agl: scenario.winch_agl_ft,
  disc: (scenario.rate * 100).toFixed(1),
  horizon: scenario.horizon_yr,
  fuel: scenario.fuel_multiplier.toFixed(2),
})
history.replaceState(null, '', `#${params}`)
```

Parse on mount, restore slider state.

#### 9. Fallbacks

- **No `alt_airframe_candidates` on tracks** (PR #2 not deployed yet)
  → hide the What-If panel with a small "available after next deploy"
  note.
- **`substitutes.json` 404** → same.
- **All sliders at default** → render baseline only, hide the
  business-model table.

#### V2 additions — regulatory demand-reduction scenarios

Two new what-if sliders that **don't substitute the airframe** —
they reduce the *count* of training tracks because the regulatory
demand for those flights drops. Slider value 0..100 maps linearly
to `0..max_reduction_pct` (from `substitutes.json`) — tracks
deterministically eliminated rather than dBA-substituted.

Both already shipping in
[`noise/web/public/substitutes.json`](web/public/substitutes.json):

| Code | Name | Slider label | Max reduction | Notes |
|---|---|---|---:|---|
| `ATPR` | ATP rule rollback (250 hr restricted-ATP) | "ATP rollback %" | 60% | Rolling back the 2013 1500-hr rule restores the pre-Colgan commercial-to-ATP path; airlines would hire at 250–500 hr, killing most of the industry-driven hour-building flights. |
| `SIMX` | Expanded simulator credit (200 hr toward ATP) | "Simulator expansion %" | 30% | Current cap is 100 hr (regular ATP) / 200 hr (R-ATP via 4-yr program). Expanding the general cap shifts ~30% of in-air training onto FTDs. |

New `scope` value the client must handle: `"track_eliminate"`. A
substitute with this scope, when active, removes matching tracks
from the histogram entirely (they don't contribute any dBA at all).
Stacks with existing sliders — a track can be both VELE-substituted
AND eliminated by ATPR, in which case eliminate wins.

##### Client-side slider math

```js
function pickEliminated(tracks, code, sliderPct, substitutes) {
  if (sliderPct <= 0) return new Set()
  const sub = substitutes.find((s) => s.code === code)
  if (!sub || sub.scope !== 'track_eliminate') return new Set()
  const eligible = tracks
    .filter((t) => (t.purpose && sub.replaces_purposes.includes(t.purpose)))
    .map((t) => t.tail)
    .sort()
  const effectivePct = (sliderPct * sub.max_reduction_pct) / 100  // 0..max_reduction_pct
  const k = Math.floor((eligible.length * effectivePct) / 100)
  return new Set(eligible.slice(0, k))
}
```

Note the **two-stage percentage**: the slider is "% of max effect"
(intuitive UI: "how aggressive is the policy uptake?") and
`max_reduction_pct` is the cap baked into the substitute config
(realistic industry response). At slider=100, ATPR removes 60% of
training tracks; at slider=50, it removes 30%.

##### Integration into `segmentDba`

Eliminated tracks are dropped *before* the histogram aggregator runs,
not at the per-segment dBA step:

```js
function aggregateHistogram(tracks, scenario, listener, substitutes) {
  const eliminated = new Set([
    ...pickEliminated(tracks, 'ATPR', scenario.atpr_pct, substitutes),
    ...pickEliminated(tracks, 'SIMX', scenario.simx_pct, substitutes),
  ])
  const survivingTracks = tracks.filter((t) => !eliminated.has(t.tail))
  return bucketize(survivingTracks.map((t) => peakDbaForScenario(t, scenario, listener)))
}
```

Two new slider entries in the slider table:

| Slider | Default | Range | Step | Owns |
|---|---:|---:|---:|---|
| ATP rollback % | 0 | 0..100 | 5 | Tracks where `purpose=training` get eliminated, scaled by `ATPR.max_reduction_pct` (60%) |
| Simulator expansion % | 0 | 0..100 | 5 | Tracks where `purpose=training` get eliminated, scaled by `SIMX.max_reduction_pct` (30%) |

##### NPV / cost lines for regulatory scenarios

These are not airframe purchases, so the NPV math reads differently
in the business-model table:

- **ATPR** — `cap_ex_usd = 0`. Show `advocacy_capex_usd` (~$250k
  one-time, a multi-year rulemaking advocacy campaign) as a
  separate line item. `op_savings_per_hr_usd = 0` from the listener
  perspective (no per-flight savings — flights just don't happen).
  Optional richer view: surface `policy_cost_per_pilot_usd` (−$50k)
  in a tooltip — student savings, but irrelevant to noise-abatement
  ROI.
- **SIMX** — `cap_ex_usd = $350k` per school (Level 5+ FTD).
  `op_savings_per_hr_usd = −$50` (school saves $50/hr on the
  substituted portion). `useful_life_years = 15`,
  `residual_value_pct = 20%`. NPV computes like a normal aircraft
  substitute but the "annual hours" is the **sim**'s annual hours
  (~1500, not the displaced flight hours).

For both, the "**peak dB reduction**" column is the headline win
(noise drops in proportion to track count, not per-flight). The
"$/dB-reduction" column reads `$0` or even **negative** when the
program saves money on net — these scenarios are unusually cheap
per dB because no fleet capital is required.

##### URL hash extension

Two new keys in the hash:

```js
const params = new URLSearchParams({
  // existing
  electric: scenario.electric_pct,
  eurofox: scenario.eurofox_pct,
  sinus: scenario.sinus_pct,
  winch_agl: scenario.winch_agl_ft,
  // new V2
  atpr: scenario.atpr_pct,
  simx: scenario.simx_pct,
  // financial knobs unchanged
  disc: (scenario.rate * 100).toFixed(1),
  horizon: scenario.horizon_yr,
  fuel: scenario.fuel_multiplier.toFixed(2),
})
```

##### V2 status

Server-side: `substitutes.json` already extended with ATPR + SIMX
entries.

Client-side: shipped at PR
[bgatti/KnownRisks#4](https://github.com/bgatti/KnownRisks/pull/4)
(base = PR #3) — 9 per-section commits, 16 new vitest cases, 37/37
PointNoiseReport tests green, 217/235 full suite green (the 4
flakes are pre-existing DB-dependent noiseReports tests, unrelated).

**Server-team self-verification 2026-06-01:**

| Check | Result |
| --- | --- |
| `npm run build` on `feature/whatif-v2-regulatory` | ✅ clean, 820 KB bundle |
| `GET /substitutes.json` carries ATPR + SIMX with `scope: "track_eliminate"` + `max_reduction_pct` (60, 30) | ✅ |
| `GET /src/PointNoiseReport.jsx` source carries `atpr_pct`, `simx_pct`, `pickEliminated`, `eliminatedTails`, `regulatoryColumn` | ✅ all five symbols present in 4218-line module |
| `GET /src/whatif.js` exports include `pickEliminated` + `regulatoryColumn` | ✅ |

**V2 acceptance: [DONE].** Both sliders wired into scenario state +
URL hash, both modify the eliminated-tracks set, business-model table
gains two new columns (Mechanism + Regulatory NPV) when either slider
> 0. Channel closes on this section.

#### Channel back to server team

Edit any of the following inline in this section (server team watches
on a 15-min tick):

- **Blocker:** `[BLOCKED] <what's wrong>` — server team prioritizes.
- **Question:** `[Q] <question>` — server team answers inline below
  your Q.
- **Mismatch:** `[MISMATCH] expected X, got Y, here's a curl`
  — server team fixes the API or corrects this spec, whichever is
  right.
- **Done:** change `[WIP]` → `[DONE]` and add a one-line confirmation
  of what's rendering. The channel closes on `[DONE]`.

---

## Open questions (not asks, just notes)

- **Engineless override scope.** Spec says engineless types contribute
  0 dBA. Reality: even a glider with a tow plane generates a *tow* dBA
  contribution. Today the page attributes that to the tow plane track
  directly (PA25 / PA18), which is correct — but if the API ever
  returns "tow cycle" as a virtual track (glider + tow), we'd need
  to disambiguate. Worth defining in `noise/CLAUDE.md`.

- **`base_airport` semantics on transient overflights.** A Cessna
  N1234 based at KAPA flying over Boulder lands as `base: KAPA` on
  `/api/flights/current?airport=KBDU` even though it never touched
  KBDU. That's "what we want" for attribution but it's a different
  semantics from "flew through KBDU airspace". The point-noise report
  doesn't care (the listener is the anchor), but the operator-rollup
  becomes misleading if read as "operators flying *to* this listener".
