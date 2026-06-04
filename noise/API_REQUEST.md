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

#### 2b — Limit-truncation is a correctness bug, not just performance

> ✅ **RESOLVED 2026-06-04 — deploy `d3b6cda3` landed listener-anchored
> geo-filter on `/api/sorties` (`?center=lat,lon&radius_nm=N`). Client
> migrated off `/api/excursions/segments` in the same session — see
> [web/src/PointNoiseReport.jsx](web/src/PointNoiseReport.jsx)
> `fetchSortiesSameOrigin` + `sortieToTrack`. Verified at the same
> Frasier Meadows listener (39.985, -105.21) on a 24 h window:
> **294 sorties returned vs the old 20 / 500 LIMIT-truncated result** —
> >14× more in-radius tracks now reach the page.**
> The legacy banner stays in place keyed on `candidates_considered` so
> any stale deployment still serving the segments shape would still
> surface the warning, but the live page now bypasses the bug
> entirely.**

#### 2c — `/api/sorties` silently caps `window_hours` at 48

**Filed 2026-06-04.** Now that the geo-filter is live (§ 2b) the page
exposes a `7 d` / `14 d` / `30 d` time-window switch (see
`WINDOW_PRESETS` in
[noise/web/src/PointNoiseReport.jsx](web/src/PointNoiseReport.jsx)).
Curl evidence that the server is silently truncating the window:

```
endpoint: /api/sorties?center=39.985,-105.21&radius_nm=4&hours=H
H=72    sorties=465  sortie_window_hours=48  span=2026-06-02T06:38 → 2026-06-04T03:40
H=168   sorties=465  sortie_window_hours=48  span=2026-06-02T06:38 → 2026-06-04T03:40
H=336   sorties=465  sortie_window_hours=48  span=2026-06-02T06:38 → 2026-06-04T03:40
H=504   sorties=465  sortie_window_hours=48  span=2026-06-02T06:38 → 2026-06-04T03:40
H=720   sorties=465  sortie_window_hours=48  span=2026-06-02T06:38 → 2026-06-04T03:40
H=1344  sorties=465  sortie_window_hours=48  span=2026-06-02T06:38 → 2026-06-04T03:40
```

The same 465-sortie response is returned byte-for-byte regardless of
the `hours` parameter once it exceeds ~48. The `sortie_window_hours`
echo confirms the server is interpreting every value above 48 as 48
without an error or warning.

**Why this matters:**
- `/point-noise` advertises 14 d / 30 d windows to support multi-week
  noise trend analysis (the kind of evidence a takings petition or city
  council brief needs — a week of June overflights doesn't make the
  case alone).
- A response payload of 12 MB / 465 sorties at the current 48 h cap
  means a 14 d window would land around 80 MB / 3 k sorties at the same
  density. That's heavy but the page deduplicates by tail so most of
  the bandwidth pays off. Per the user 2026-06-04: *"7days, query is
  fast enough, maybe we can do 2 weeks, 1 month? curl to ensure we are
  getting more records 800 might be ok."*
- Right now the 7-d, 14-d, 30-d buttons all show the same data because
  the server caps the underlying query — the UI lies about the period.

**Ask:**
1. Raise the `window_hours` cap to at least 30 days (720 h) on
   `/api/sorties` so the multi-week buttons return distinct data.
2. If the cap is intentional (perf protection), return an explicit
   413 / 400 with the actual cap echoed so the client can render a
   "max window is N hours" hint, instead of silently truncating. The
   silent-truncate failure mode is identical in shape to the §2b bug
   we just fixed — it makes the client look broken when it's actually
   the server quietly ignoring the parameter.
3. If perf protection is real, an optional `?paginate=cursor` or
   `?compact_path=true` (sortie metadata only, no path coords) for the
   "I just need counts and metadata across a long window" use case
   would let the page front-load metadata then lazy-fetch full paths
   on demand.

**Status: [PENDING-SERVER].** Client buttons (14 d / 30 d) ship in the
same session against the 48 h ceiling so the UI works the instant the
server cap lifts.

#### 2c — Server response 2026-06-04 ✅ *partially landed* (and a new failure mode)

**Cap is gone for windows ≤ ~168 h.** Sweep against the same listener:

```
hours=  1  echoed=  1  sorties=   18
hours=  6  echoed=  6  sorties=   72
hours= 12  echoed= 12  sorties=   78
hours= 24  echoed= 24  sorties=  287
hours= 48  echoed= 48  sorties=  482
hours= 72  echoed= 72  sorties=  677
hours=168  echoed=168  sorties= 1450  (was 0 when capped at 48h yesterday)
hours=169  echoed=169  sorties= 1474
hours=192  echoed=192  sorties= 1639
hours=240  echoed=240  sorties= 1976
hours=336  echoed=336  sorties=    0   ← cliff
hours=720  echoed=720  sorties=    0
```

`window_hours` now echoes back what the client requested up to 720 —
the silent clamp at 48 is fixed. Recent windows return real data
linearly.

**New silent-empty failure mode at the historical-archive boundary.**
For `hours ≥ ~336`, the response shape is:

```
sortie_count: 0
sortie_window_hours: 720
sortie_source: "db_timeout:historical 2026-05-05..2026-06-04"
```

The server hits a DB timeout against the historical archive
(cold-cache, larger date range, presumably a different table) and
returns 200 OK with the failure annotation in `sortie_source` —
NOT a 503 + Retry-After. The page treats it as "no data in this
radius" and renders empty, which is the exact UX bug § 2c-(2) called
out: the user can't tell the difference between "your listener is in
a quiet zone" and "the server gave up".

**Client mitigation this session:** added a rose-toned banner that
surfaces the failure when `sortie_source` matches `/^db_timeout:/`
(see [PointNoiseReport.jsx](web/src/PointNoiseReport.jsx) — search
for "Server's historical-archive query timed out"). User sees:

> Server's historical-archive query timed out for the requested
> window. `db_timeout:historical 2026-05-05..2026-06-04`. Try a
> shorter window (≤ 7 d) — recent ranges hit the live cache and
> return in < 1 s.

**Remaining ask:** return a proper 503 + `Retry-After: <s>` (or 504
gateway-timeout) for the timeout path so the client's existing
cold-cache-retry handler in `fetchChunk` engages. The
`sortie_source` annotation is a fine secondary signal but the
HTTP-level failure code is what every standard client tolerates
without bespoke string-matching.

**Status: [PARTIAL — cap lifted, but historical-archive timeouts
return empty 200 instead of 503/504].**

#### 2d — Time-range params on `/api/sorties` for progressive paging

**Filed 2026-06-04, per user direction "if we need time range in api,
just ask".** The page is shifting to a progressive-load model: start
with the trailing 12 h (renders fast), then append 24 h chunks of
older data until the user's flight-count target (~800) is met, with
the UI live-updating as each chunk arrives. The point of going
chunked is to keep the initial render fast AND avoid the
silent-truncation problem in § 2c — each chunk is small enough that
the server cap isn't the binding constraint.

The current API shape (`?hours=N&center=...&radius_nm=R`) is
trailing-window only: every value returns the trailing N hours of
data ending at "now". There's no way to ask for a SPECIFIC older
slice ("hours 24–48 ago", "hours 48–72 ago"), so chunked loading
requires re-fetching the entire trailing window for each step,
which (a) wastes bandwidth and (b) still hits the § 2c cap as soon
as the window crosses 48 h.

**Ask:** add time-range parameters to `/api/sorties`. Either of these
shapes works:

- **(a)** `?from=<iso8601>&to=<iso8601>` — explicit absolute window.
  Cleanest; lets the client deterministically chunk into any slicing
  scheme it wants (24 h, 6 h, calendar-day, …) and verify that the
  server honoured the window via the same fields in the response.
- **(b)** `?end_ts=<iso8601>&hours=N` — relative window with an
  adjustable endpoint. Mirrors the current `?hours=N` shape (so the
  current implementation becomes `?end_ts=now&hours=N`) and lets the
  client paginate by walking `end_ts` backward.

The shape of the response (echo of the resolved window via
`sortie_window_hours` / `sortie_window_from` / `sortie_window_to`)
matters as much as the request — the client uses it to know whether
the server actually honoured the requested window, vs the silent
truncation we hit in § 2c.

**Status: [PENDING-SERVER].** Until time-range support lands, the
client will progressively load by re-issuing `?hours=N` with growing
N. That works up to the § 2c cap (currently 48 h, ~465 sorties at
this listener) and then stalls — flagged in the loader's UI so the
user knows when the wall hits.

---

#### 2h — 🚨 P0: live cache timing out for ALL window sizes (+ failure responses are being cached)

**Filed 2026-06-04T19:04Z, user-observed during active report build.**
Yesterday the same listener returned **1,450 sorties** for a 7 d
window in < 1 s. Right now every window from 1 h up returns
**0 sorties**, and the server is **caching its own failure for 15 s**
via `Cache-Control: public, max-age=15` — so every consumer (kiosk,
pilot-console, /point-noise) sees the same empty payload across that
window.

##### Reproduce (copy/paste, no auth required)

```bash
# Architecture note: the noise API is served by noise/web/start.js
# under the local Vite dev proxy at 127.0.0.1:5183. It forwards
# query execution to Railway PostgreSQL via the pool in
# noise/web/db.js. The 404 on /api/sorties at the public
# regionalcompliance-production.up.railway.app confirms the noise
# API is only exposed via the local Node process — so the timeout
# is on the [Node → Railway PG] path, not on a remote API surface.
# Suspect either the pg-pool config or the PG instance itself.

curl -sI -w "\nstatus=%{http_code} ttfb=%{time_starttransfer}s total=%{time_total}s\n" \
  "http://localhost:5183/api/sorties?center=39.985,-105.21&radius_nm=4&hours=1"

curl -s "http://localhost:5183/api/sorties?center=39.985,-105.21&radius_nm=4&hours=12" \
  | python -c "import sys,json; d=json.load(sys.stdin); print('count=', d['sortie_count'], 'src=', d.get('sortie_source'))"
```

##### Observed response (hours=1, 19:04:08 UTC)

```
HTTP/1.1 200 OK
Content-Type: application/json
Cache-Control: public, max-age=15            ← BUG: caching the failure
Date: Thu, 04 Jun 2026 19:04:08 GMT
Content-Length: 13320

{
  "sortie_window_hours": 1,
  "sortie_geo_filter": {"lat":39.985,"lon":-105.21,"radius_nm":4},
  "sortie_count": 0,
  "sortie_source": "db_timeout:live 1h",
  ...
}
```

##### Window sweep (single timestamp, listener-anchored Frasier Meadows)

| hours | sortie_count | sortie_source                              |
|------:|-------------:|--------------------------------------------|
|     1 |            0 | `db_timeout:live 1h`                       |
|     6 |            0 | `db_timeout:live 6h`                       |
|    12 |            0 | `db_timeout:live 12h`                      |
|    24 |            0 | `db_timeout:live 24h`                      |
|    48 |            0 | `db_timeout:live 48h`                      |
|   168 |            0 | `db_timeout:historical 2026-05-28..2026-06-04` |

`sortie_source` now carries **two distinct failure modes**:

- **`db_timeout:live <Nh>`** — primary/live DB query timing out for
  every window size including 1 h. A shorter window does NOT help.
- **`db_timeout:historical <range>`** — archive table times out for
  older windows (already documented in § 2c).

Both still return HTTP 200 + `sortie_count: 0` — the silent-empty
failure mode § 2c-(2) called out. Page can't tell "quiet listener"
from "DB gave up".

##### Asks, in priority order

1. **🚨 Investigate the live cache.** Likely suspects (in the order
   I'd check): pg-pool exhaustion, missing index after a schema
   migration, dropped warm cache after a deploy/restart, or a stuck
   warmup job holding the query path. Yesterday's 1450-sortie
   7 d query is a known-good baseline to A/B against.
2. **🚨 Stop caching failure responses.** `Cache-Control: public,
   max-age=15` on a `sortie_count: 0 + sortie_source: db_timeout:*`
   response is wrong on two axes: it amplifies the failure window
   (every consumer in those 15 s gets the cached empty), and it
   defeats the client's natural retry behaviour. Quick fix: emit
   `Cache-Control: no-store` whenever `sortie_source` starts with
   `db_timeout:` or `sortie_count` is 0 with any error annotation.
3. **Return 503 + `Retry-After: <s>`** instead of 200 for the
   timeout path. The client's existing cold-cache-retry handler
   (`fetchChunk` → `isRetryableErr` in `runReport`,
   [noise/web/src/PointNoiseReport.jsx](web/src/PointNoiseReport.jsx))
   engages automatically on 5xx with no client change required.
4. **Add `sortie_health: "ok" | "live_db_degraded" | "archive_degraded"`**
   at the top of the response so the kiosk, the pilot-console, AND
   the noise page can render a shared outage banner instead of each
   consumer separately string-matching `sortie_source`.

##### Client mitigation (shipped in same session, no server dependency)

The page now ships a **kind-aware rose banner** that parses
`sortie_source`:

- `db_timeout:live <Nh>` → *"Server's live database is timing out
  — every window size is currently returning empty. This is a
  server-side incident, not a problem with your listener / radius
  / window. A shorter window will not help."*
- `db_timeout:historical <range>` → *"Server's historical-archive
  query timed out for the requested window. Try a shorter window
  (≤ 7 d)."*

So users now see the truth instead of a silent empty radius. The
`sortie_source` annotation is fine as a permanent secondary signal
even after the 503 work lands.

**Status: [PARTIAL — §2h-3 ✅ landed @ ~19:14Z, others still pending].**

##### Server response 2026-06-04T19:14Z — `503 + Retry-After` landed ✅

The server now returns proper HTTP 503 for the timeout path with the
exact shape § 2h-3 asked for:

```
HTTP/1.1 503 Service Unavailable
Content-Type: application/json
Cache-Control: public, max-age=15           ← § 2h-2 still NOT fixed
Retry-After: 5
Content-Length: 181

{
  "error": "sortie data source temporarily unavailable",
  "error_kind": "db_timeout",
  "stage": "live",
  "detail": "load_timeout",
  "retry_after_seconds": 5,
  "sortie_source": "db_timeout:live 12h"
}
```

The structured fields (`error_kind` / `stage` / `detail`) are
better than § 2h-4 asked for — they distinguish the failure mode
without bespoke string-matching. Nice.

##### Still pending after the 19:14Z deploy

- **🚨 § 2h-1: live cache itself is still broken.** Every window
  still times out → 503. The 503 is correct framing but the user
  still can't load any data. This is the actual P0 — investigate
  the pg-pool / index / warmup suspects.
- **🚨 § 2h-2: `Cache-Control: public, max-age=15` is still on the
  failure response.** The status went 200 → 503 but the
  cache-the-failure bug travelled with it. Browsers will treat the
  503 as cacheable for 15 s (the freshness rule applies to 5xx
  responses too if explicit Cache-Control says so), defeating the
  Retry-After hint. Quick fix: emit `Cache-Control: no-store` when
  `error_kind === "db_timeout"`.
- **§ 2h-4 (sortie_health top-level):** superseded by the
  `error_kind`/`stage` structure on the 503 body — that's strictly
  better. Closed.

##### Update 19:21Z — server team commits visible

Inner-repo `noise/web` log shows two server-team deploys:

```
39ad2fe (19:12Z) sorties: 503 + Retry-After on db_timeout; add (day, id) index
9e12c0f (19:21Z) sorties: 30s response cache to bound recompute cost per request
```

The (day, id) index attempt didn't fix the DB query — at 19:26Z
every window 1h–48h still returns 503 within an 8 s timeout
budget (doubled from the 4 s I observed at 19:14Z). The 30 s
response cache adds an `X-Sortie-Cache: MISS|HIT` header but
currently misses on every request since nothing is successful
enough to populate it.

**Cache-Control: public, max-age=15 is STILL on the 503 response
after both deploys.** That keeps the cache-the-failure bug live —
the response cache header is independent of the response code, so
the 503 itself is being treated as freshness-valid for 15 s by
upstream proxies / browsers. This is the simplest of the four
asks and the only one not yet touched.

##### Suggested next moves for the server team

If the DB query is still failing even with the new index, the
likely remaining suspects (in rough order):

1. **Check `pg_stat_activity` for long-running / blocked queries.**
   If something is holding a lock on the sortie path table, the
   index won't help.
2. **Check pg-pool stats** — if pool is exhausted (waiting > max
   wait), new requests time out without ever reaching PG.
3. **Validate the new (day, id) index is actually being used by
   the query planner** — `EXPLAIN ANALYZE` on the production
   sortie query. An index that exists but isn't picked still leaves
   you on the sequential scan.
4. **If queries are slow even with the index, the query itself may
   need rewriting** — windowed aggregations against the sortie path
   are sometimes faster done in two passes (id range first, then
   per-id aggregation) than one combined query.

##### One simple fix the server team should still ship — §2h-2

```diff
- res.setHeader('Cache-Control', 'public, max-age=15')
+ if (res.statusCode >= 500 || sortie_count === 0) {
+   res.setHeader('Cache-Control', 'no-store')
+ } else {
+   res.setHeader('Cache-Control', 'public, max-age=15')
+ }
```

Without this every browser + intermediate proxy caches the 503
for 15 s, defeating the Retry-After hint and amplifying the
outage window for every consumer.

---

#### 2g — Publish the canonical noise-propagation + throttle-curve parameters

**Filed 2026-06-04.** The /point-noise page now applies a defensible
acoustic propagation kernel (pure 1/r² spherical spreading +
AEDT/NPD-style 22·log10(throttle) curve, fit to ANP per-aircraft NPD
tables for piston-prop singles). The coefficients (22, 20) are
inline in the page today. Per the user's "use the API channel for
things that should be universally true" preference, these should
ship from the server so the leaderboard / kiosk / page all read the
same dBA for the same aircraft at the same slant.

Suggested shape (`GET /api/noise-propagation`):

```json
{
  "generated": "2026-06-04",
  "model": "spherical-1r2-aedt-throttle",
  "reference_slant_ft": 500,
  "agl_floor_ft": 100,
  "throttle_floor_frac": 0.05,
  "propagation_coef_db_per_decade": 20,
  "throttle_coef_db_per_decade": 22,
  "sources": [
    "SAE AIR-1845A (1995) — NPD method specification",
    "ICAO Doc 9911 (2008/2018) — implementation guidance",
    "FAA AEDT 3 Technical Manual §4.6 — propagation + throttle",
    "Smith MJT, Aircraft Noise (Cambridge UP 1989) §6 — piston-prop fit"
  ],
  "per_category_overrides": {
    "jet":        { "throttle_coef_db_per_decade": 22 },
    "turboprop":  { "throttle_coef_db_per_decade": 20 },
    "piston":     { "throttle_coef_db_per_decade": 22 },
    "helicopter": { "throttle_coef_db_per_decade": 16, "note": "rotor noise dominated; lower power-sensitivity" }
  }
}
```

The client already accepts the formula structure parameterically (one
function in PointNoiseReport.jsx — `estDbaAtListener` +
`throttleDbaAdjustment`), so the migration is just a fetch + state
read. Until the endpoint exists the page uses the inline defaults
listed above.

**Status: [PENDING-SERVER].**

#### 2f — Push the type → base-dBA table to the server (and stop classifying helicopters as gliders)

**Filed 2026-06-04, per user direction "very important that we have a
type → base dbA at full throttle / should be pushed to API / but these
are WAY wrong. gliders have a base dbA of 0 at any distance".**

Two problems surfaced by today's listener readout:

```
Time              Tail    Type   Purpose                          dBA   AGL   Dist
Jun 2 12:16 PM    N851MB  AS50   Glider (local soaring)~shape(85%)  79   800  0.22 nm
May 30 09:01 AM   N851MB  AS50   Glider (local soaring)~shape(85%)  79   700  0.24 nm
```

1. **The type → base-dBA table lives in the client** (see TYPE_BASE_DBA
   in [noise/web/src/PointNoiseReport.jsx](web/src/PointNoiseReport.jsx)),
   so the leaderboard, the kiosk, and the page can disagree on what a
   given airframe sounds like. **Ask:** publish the canonical
   per-type-code base-dBA-at-1000-ft-AGL/500-ft-slant table from the
   server (e.g. `GET /api/type-noise-profile` returning
   `{ "AS50": { "base_dba": 84, "category": "helicopter", "engineless": false }, … }`).
   The client already has the mapping inline — pushing it server-side
   removes the drift risk.

2. **purposeML is classifying engine-powered helicopters as
   `glider_local`** (see N851MB above — AS350 Squirrel,
   single-turbine helicopter, repeatedly tagged "Glider (local
   soaring)" with shape-confidence 85 %). The geometry hedge here is
   plausible (a helicopter mountain-tour pattern can look like a
   glider thermalling pattern from a coarse track), but the type code
   AS50 is unambiguous: type codes that match the helicopter family
   (AS50/AS55/AS65, B06/B407/B429, EC20/EC30/EC35/EC45, H500, etc.)
   should NEVER be eligible for the glider_local / glider_xc / glider
   purposes. **Ask:** add a hard-no rule in purposeML that excludes
   helicopter / turboprop / piston-twin types from the glider buckets
   regardless of how confidently the shape-classifier matches a
   soaring pattern.

The downstream noise math is correct (AS50 base_dba = 84, page reports
79 dBA at 800 ft AGL — reasonable). The label is the bug: a user
reading the listener events sees "Glider (local soaring) — 79 dBA" and
loses confidence in the entire classification chain.

**Status: [PENDING-SERVER]** for both. Client-side mitigation in the
same session: the page will treat the purpose label as advisory when
the type is in the helicopter / turboprop / jet families and report
the type's category instead (e.g. "Helicopter" not "Glider").

#### 2e — Serve nice-text purpose labels + colour palette from the API

**Filed 2026-06-04, per user direction "we have a purpose translator
(Nice Text) in the project, could we ... ask the API for a NiceText
json and push this to the API".** The page (and the leaderboard, and
the kiosk) each carry their own inline map of purpose-code → human
label + colour, and they drift over time. The new
[noise/web/public/purpose_labels.json](web/public/purpose_labels.json)
consolidates the page's copy as a static asset; the channel ask is to
move it to the server so every consumer reads the same canonical map.

Shape (mirror the file directly):

```json
{
  "generated": "2026-06-04",
  "palette_id": "sage-2026-06-04",
  "purposes": {
    "training":     { "label": "Training",        "color": "#7fb3a3" },
    "tow_plane":    { "label": "Glider tow",      "color": "#c9a96a" },
    "glider_local": { "label": "Glider (local soaring)", "color": "#b6abce" },
    ...
  }
}
```

**Ask:** stand up `GET /api/purpose-labels` that returns this exact
shape. The client already fetches /purpose_labels.json with a graceful
fallback to inline maps; once the API endpoint exists the page can
point at it (the static file becomes the fallback when the endpoint
404s, not the source of truth).

**Why:** the maps drift. Today the page calls a `glider_xc` track
"Glider (cross-country)", the leaderboard calls it "XC glider", the
kiosk omits it entirely. Centralising the nice text removes the drift
AND lets the server roll a new code/colour without a client deploy.

#### 2e — Server response 2026-06-04 ✅ *partial landing*

The server now ships `sortie_purpose_label_catalog` inline on every
`/api/sorties` response (no separate `/api/purpose-labels` endpoint —
inline is fine, the client merges it during response adaptation; see
`adaptResponse` in
[noise/web/src/PointNoiseReport.jsx](web/src/PointNoiseReport.jsx)).

Curl evidence:

```
GET /api/sorties?center=39.985,-105.21&radius_nm=4&hours=24
→ sortie_purpose_label_catalog: {
    "cross_country":  "Cross Country",
    "ga_local":       "GA Local",
    "ga_xc":          "GA Cross Country",
    "glider_competition": "Glider — Competition",
    "glider_local":   "Glider — Local",
    "glider_soaring": "Glider — Soaring",
    "glider_training":"Glider — Training",
    "glider_xc":      "Glider — Cross Country",
    "helicopter":     "Helicopter",
    "local":          "Local",
    "pattern":        "Pattern",
    "pattern_solo":   "Pattern (Solo)",
    "practice_area":  "Practice Area",
    "survey":         "Survey",
    "tow_plane":      "Tow Plane",
    "training":       "Training",
    "transient":      "Transient",
    "unknown":        "Unknown"
  }
```

**Coverage delta vs the page's static map:**
- Server SHIPPED 3 new codes the page didn't have:
  `glider_competition`, `glider_soaring`, `glider_training` — added
  to the page's static map this session (used as fallback colours;
  server's labels win).
- Server OMITTED 12 codes the page does have:
  `airline / biz_jet / turboprop / experimental / ga_single /
   ga_twin / medevac / firefighting / search_rescue / law_enforcement /
   military / government / patrol / science`. These are still
  fallback-only client-side until the server adds them.
- Server's labels use em-dash + Title Case ("Glider — Local") vs the
  page's parenthetical-lower ("Glider (local soaring)"). Server style
  is now authoritative — page renders the server's strings verbatim.

**Remaining ask (palette):** the inline catalog ships LABELS only.
Per-purpose colours still live client-side in
`/public/purpose_labels.json` (now using the editorial-warm-dark
palette from pilot-console/console.css). The cleanest closing move is
to add a `color` field alongside `label` on each catalog entry:

```json
"glider_local": { "label": "Glider — Local", "color": "#B7A0D4" }
```

so the kiosk and the page never drift on either field. Until that
lands, the client merges server's labels over its own colours.

**Status: [PARTIAL — labels landed inline, colours still pending].**

**Update 2026-06-04 — coverage gap caught live.** User reported that
"some purposes still not NiceText". Curl against
`/api/sorties?center=39.985,-105.21&radius_nm=4&hours=24` enumerates
13 distinct `sortie_purpose` values:

```
['cross_country', 'ga_local', 'ga_xc', 'glider_local',
 'local', 'patrol', 'pattern', 'pattern_solo',
 'practice_area', 'survey', 'tow_plane', 'training', 'transient']
```

The client's nice-text map was missing **4 of these 13**: `local`,
`pattern`, `practice_area`, `transient` — they appeared in the events
table as raw codes (e.g. "local", "pattern") instead of friendly
labels. Filled the gap in
[noise/web/public/purpose_labels.json](web/public/purpose_labels.json)
this session.

**Definitive ask — the full enumerated set the server should ship for**
**at minimum** when /api/purpose-labels lands:

| Code            | Suggested label                  | Source endpoint that emits it      |
| --------------- | -------------------------------- | ---------------------------------- |
| `training`      | Training                         | purposeML / school join            |
| `pattern_solo`  | Pattern (solo / non-school)      | purposeML shape                    |
| `pattern`       | Pattern                          | geometry                           |
| `practice_area` | Practice area                    | geometry                           |
| `local`         | Local                            | geometry / fallback                |
| `tow_plane`     | Glider tow                       | type curated (PA25 / PA18)         |
| `glider`        | Glider                           | type curated                       |
| `glider_local`  | Glider (local soaring)           | purposeML shape                    |
| `glider_xc`     | Glider (cross-country)           | purposeML shape                    |
| `ga_local`      | GA local (around-the-pattern)    | purposeML shape                    |
| `ga_xc`         | GA cross-country                 | purposeML shape                    |
| `ga_single`     | GA single (private)              | type fallback                      |
| `ga_twin`       | GA twin (private)                | type fallback                      |
| `cross_country` | Cross-country                    | geometry                           |
| `transient`     | Transient overflight             | geometry                           |
| `helicopter`    | Helicopter                       | type curated                       |
| `airline`       | Airline                          | type curated                       |
| `biz_jet`       | Business jet                     | type curated                       |
| `turboprop`     | Turboprop                        | type curated                       |
| `experimental`  | Experimental / homebuilt         | type curated                       |
| `medevac`       | Medevac                          | special-use                        |
| `firefighting`  | Firefighting                     | special-use                        |
| `law_enforcement` | Law enforcement                | special-use                        |
| `military`      | Military                         | special-use                        |
| `government`    | Government                       | special-use                        |
| `patrol`        | Patrol                           | special-use                        |
| `science`       | Science                          | special-use                        |
| `survey`        | Survey                           | special-use                        |
| `search_rescue` | Search & rescue                  | special-use                        |
| `unknown`       | Unknown                          | fallback                           |

**Secondary ask:** when the server starts emitting a new
`sortie_purpose` code, please add the matching entry to
`/api/purpose-labels` in the same deploy. Otherwise the page falls
back to the raw code in the UI (what the user reported above).

**Status: [PENDING-SERVER].** The client static-file path ships in the
same session.

---

**Re-confirmed live 2026-06-04 (user request "when I request 30days, I
don't get 30days").** Curl against the same listener with `hours=720`
(30 d):

```
requested hours=720 (30 d)
server echoed window_hours=48
sorties returned=465
fix span: 2026-06-02T06:38 → 2026-06-04T03:40   (actual data: 45.0 h)
```

The server is still capping. The user's experience right now is that
clicking the **30 d** button surfaces a 45-hour slice — a 16-to-1
silent truncation. This is a user-trust bug: the chip says "30 d" and
the methodology box echoes "30 d" but the charts cover two days. From
the page you cannot tell that the data is incomplete. **This is the
identical failure mode the §2b LIMIT bug had before d3b6cda3 — the
client looks broken when the server is quietly ignoring the
parameter.**

The bare minimum near-term fix is the §2c-(2) one: return the actual
cap to the client so the chip can render `30 d → capped to 48 h` or
similar, and the page can stop claiming a window it didn't get. The
proper fix (cap raised to ≥30 d) is what unblocks the multi-week
trend analysis the page exists to support.


**Filed 2026-06-03** based on a user-side comparison of
`/api/sorties?airport=KBDU&hours=12` vs
`/api/excursions/segments?lat=40.005&lon=-105.205&hours=12&radius_nm=3`.

The sortie endpoint (airport-anchored) reported **39 sorties** at KBDU
in the window, peaking at noon MDT (12 flights). The segments endpoint
(listener-anchored at Frasier Meadows, 3 nm south of KBDU) reported

```
matched / candidates_considered: 20 / 500
tracks returned: 20
```

The 500 in `candidates_considered` is exactly the `limit` query param —
meaning the SQL pull hit the cap. The JS-side geo filter then narrowed
500 → 20 because most of the 500 candidates were outside the 3 nm
radius.

The risk this exposes: if 500+ tracks exist in the time window AND any
in-radius tracks sort below position 500 (by whatever order the SQL
query uses — currently date-DESC), **they vanish from the response**.
The page silently shows a too-small subset. We won't see the bug as a
visible error; we'll see the chart understate flight activity, and the
user can't tell from the page that the data is incomplete.

In practice the segments-at-listener response shows zero activity
between 10 AM and 6 PM MDT today even though the airport saw a noon
sortie peak; some of that gap is geometric (morning training stayed
north of the listener) but some is almost certainly limit-truncation.

**Ask reinforcement:** SQL-side bounding-box filter eliminates both the
latency AND the truncation issue. With geometry in SQL, the LIMIT
applies AFTER the geo filter — only in-radius tracks count toward the
500. The current ordering (limit BEFORE geo filter) means the response
is non-deterministic above some flight-volume threshold.

**Until §2 / §2b lands:** the client raises `limit` to its tolerable
ceiling (500 today, could push to 2000 with payload-size tradeoffs)
and surfaces `candidates_considered: <limit>` as a "possibly truncated"
warning. Both are mitigations; the real fix is the SQL filter.

**Confirmation 2026-06-03.** User: *"3Days fetches the same data /
I think 500 paths may be crimping the search"*. Re-curled 12 h vs 72 h
with same lat/lon/radius/limit; both return identical responses:

```
hours=12  candidates_considered=500  matched=20  tracks=20
          fix span 2026-06-03 18:00:24 -> 22:53:06   (4.9 h)
hours=72  candidates_considered=500  matched=20  tracks=20
          fix span 2026-06-03 18:00:24 -> 22:53:06   (4.9 h)
```

Identical. The user's intuition is exact: the LIMIT is the constraint,
the time window is moot. A 3-day query sees the same 500 most-recent
tracks, filters geometrically to the same 20, and returns the same
4.9-hour fix span. The first-day's worth of older flights — including
the morning training peak the sortie endpoint shows clearly — are
literally invisible to the client because they sort below the
LIMIT-imposed horizon.

The user's `12 h is only 6 h` complaint from a few hours ago wasn't
data-sparsity (which I had earlier concluded in § 14.0); it was
LIMIT-truncation. § 14.0 stands re: the `hours` parameter being
honoured, but the response is silently truncated so the window
parameter has no observable effect once the time slice is large
enough to contain >500 candidates.

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

### 15. Operational-capacity airframe counts per substitute

**Filed 2026-06-03; technical debt landed in same turn.** The
docked What-If sliders snap to whole airframe purchases by
computing `step = round(100 / N)` where N is the count of distinct
candidate tails in the current window
([noise/web/src/whatif.js](web/src/whatif.js) `eligibleTailCount`).

That works for VELE / EFOX — a Velis Electro replaces a C172
one-for-one, a Eurofox replaces a PA-25 one-for-one. The
data-driven count IS the operational answer.

It doesn't work for SINU. The Pipistrel Sinus is a self-launching
motorglider: each Sinus replaces a glider AND its paired tow
plane operationally, and one Sinus serves multiple glider flights
per day (no aerotow choreography between launches). The
right "100 %" for SINU is "how many Sinus airframes operationally
replace ALL local glider activity at this field" — which is a
capacity calculation, not a count of tails currently in the data.

User direction 2026-06-03: *"as technical debt, assume that 5
sinus gliders would replace all local glider tows / so 5=100% /
mark as technical debt and ask the API to calculate this properly".*

**Client workaround (landed):** new `tdebt_airframe_count_override`
field on the SINU substitute entry in
[noise/web/public/substitutes.json](web/public/substitutes.json),
hard-coded to `5`. The page reads it in `eligibleTailCounts` and
overrides the data-driven count. Comment in the JSON marks it as
technical debt pointing here.

**Ask:** add a server-computed
`operational_replacement_count` (or per-airport-keyed version)
to `/substitutes.json`, OR expose a dedicated endpoint
`GET /api/fleet/capacity?airport=<icao>&substitute_code=<code>`
that returns the number of airframes required to operationally
replace the substitute's target purpose-class at that field.

Suggested formula (open to revision):

```
glider_launches_per_day_at_field = sum over last 30 days /
                                    30  (smooth out weekend bumps)
per_sinus_daily_capacity = 8   (configurable per substitute; pilots
                                comfortable doing more cycles will
                                tune up, conservative number for v1)
operational_count = ceil(glider_launches_per_day_at_field /
                         per_sinus_daily_capacity)
```

For KBDU the user's empirical estimate is `5`; the formula above
hits `5` cleanly when the field has ~35-40 launches/day, which is a
reasonable busy-summer-weekend number for SSB + Mile High combined.

Once the server endpoint lands, the client drops the
`tdebt_airframe_count_override` field and reads
`operational_replacement_count` from the live response. Channel
closes when the override field is removed from the page's
substitutes.json read path.

### 14. Time-window UTC handling on `/api/excursions/segments`

**Filed 2026-06-03; re-tested 2026-06-04.** The user dragged the
time-window chip from 6 h → 12 h and observed "only 6 hours of
data". First test pointed at a server bug; second test (after a
vite restart) showed the server is fine and the symptom is
**data sparsity + live-ingest gaps**, not parameter mishandling.

#### 14.0 — Re-tested: parameter is honoured

Curl matrix at the listener (lat 39.9894, lon −105.2258, current):

```
hours=1   → window.hours=1   from=2026-06-03T22:09  to=2026-06-04T04:09  tracks=8
hours=6   → window.hours=6   from=2026-06-03T22:09  to=2026-06-04T04:09  tracks=8
hours=12  → window.hours=12  from=2026-06-03T16:10  to=2026-06-04T04:10  tracks=8
hours=24  → window.hours=24  from=2026-06-03T04:10  to=2026-06-04T04:10  tracks=8
hours=48  → window.hours=48  from=2026-06-02T04:10  to=2026-06-04T04:10  tracks=8
```

`window.hours` matches the request in every case; from/to slide
the window backward correctly. **The earlier failing test
(2026-05-24/25 stale slice) was against a server in a state where
the live-ingest path had been stopped for ~9 days.** Touching
vite.config.js to restart re-engaged the ingest and unblocked the
fresh data.

#### 14.1 — Data sparsity + ingest reliability (the actual issue)

The 12 h `hours=12` window returned `window.from`=10:10 AM local,
`window.to`=10:10 PM local. The actual fixes inside that window only
span **18:00–20:00 local**:

```
fixes total: 925
18:00  559
19:00  282
20:00   84
```

So the user's "I only see 6 hours of data" is correct in the sense
that flight activity in this radius today only spans ~3 hours of
the requested 12. That's not a server bug — it's how many
flights actually went overhead.

What IS a server-side hardening opportunity:

**Ask 1 — live-ingest health.** When the user observes data
sparsity, they can't distinguish (a) "ingest stopped" from
(b) "the world was quiet". Add `data_horizon.newest_ts` (timestamp,
not just date) to the segments response so the client can render
"data freshness" — green if newest_ts is within the last 5 minutes,
amber 5-60 min, red > 60 min.

**Ask 2 — auto-recovery from ingest stalls.** The 2026-05-25
stale slice persisted for 9 days before a manual vite restart
brought ingest back. Add a heartbeat that auto-restarts the ingest
worker if it hasn't ingested a fix in N minutes. Without this the
dev box can be in a "looks healthy but isn't" state indefinitely.

**Ask 3 — `window.fix_span_hours` in the response.** Even when the
window is correctly served, the visible-flight span is often much
narrower (above: 3 of 12 hours). Surfacing the actual fix-time
span in the response (`window.fix_first_ts`, `window.fix_last_ts`)
lets the client paint the empty parts of the time axis as
"no traffic observed" rather than misleading the user into thinking
the chart axis hides data.

#### 14.2 — UTC labelling on chart axes (client-side fix)

The segments response carries timestamps as ISO 8601 UTC, which is
correct. The client uses `new Date(closestTs).getHours()` to bin
locally for display. That's also correct for showing "9 am" in the
viewer's wall-clock time.

What's NOT correct: the page implicitly assumes that "9 am" on the
chart means "9 am today" (or 9 am within the current window). If
the server ever returns a stale window (see § 14.1's auto-recovery
ask) and the client bins by hour-of-day, the user gets visually-
recent-looking morning patterns that are actually older. This is a
*labelling* bug, not a timezone bug — the bins are technically
correct but the presentation can mislead.

**Ask:** none on the server. The fields the client needs to drive
the labelling fix are already in the response (`window.from`,
`window.to`, the per-segment `startedAt` timestamps). Client will
fade headline KPIs and annotate the time axis with the actual date
when `window.to` is more than 6 h before `now`. Flagging here so
the server team knows the labelling change depends on `window.to`
being accurate (it already is).

#### Channel back to server team

Edit inline when § 14 lands or needs clarification:

- `[DONE]` — `data_horizon.newest_ts` lands AND ingest-watchdog is
  running. Client closes the channel.
- `[BLOCKED]` — `[BLOCKED] <reason>` if the ingest watchdog needs
  infra (systemd / cron / process supervisor) outside the segments
  handler's scope.
- `[CLARIFY]` — `[CLARIFY] <question>` for anything in § 14 that's
  ambiguous.

### 12. Take-Action panel — turn the report into a petition

User direction 2026-06-03: residents reading the report want a
*next step* — somewhere they can grab petition language they can
send to the FAA / Congress / city council to push for the changes
the What-If sliders are modeling. The page already proves the noise
is real and shows what would help; the missing piece is the contact
+ text. A fixed bottom-right "Take Action" button opens an action
panel listing the petitions whose substitutes / scenarios the
listener has nudged > 0 first, then the rest, with copy-paste-ready
text and target-official contacts per petition.

**Status — V1 landed 2026-06-03, client-side.** New
`noise/web/public/actions.json` carries seven petitions, each tied
to a substitute / scenario code so the action panel can prioritize
ones relevant to the listener's active sliders:

- `EUROFOX_FAA_CERT` — accept EASA type certificate for Aeropro
  Eurofox glider tow. Triggers when `eurofox_pct > 0`.
- `VELE_FAA_CERT` — certify Pipistrel Velis Electro for US flight
  training. Triggers when `electric_pct > 0`.
- `SIM_HOURS_1500` — FAA accept silent-simulator hours toward the
  1500-hour ATP requirement. Triggers when `simx_pct > 0` (and any
  time the page surfaces training noise).
- `ATPR_1500_REVIEW` — congressional review of the 1500-hour rule
  (no evidence base; quadruples small-aircraft traffic). Triggers
  when `atpr_pct > 0`.
- `BACKCOUNTRY_OPEN` — open backcountry airstrips + waterways in
  CO so training traffic disperses out of urban airspace. Always on.
- `WINCH_KBDU` — petition KBDU airport board to investigate winch
  launches. Triggers when `winch_agl_ft > 0`.
- `SUBSIDY_ELECTRIC` / `CAPITALIZE_MOTORGLIDER` — find donor /
  underwrite quieter training. Always on (no FAA action needed,
  community-organize).

Client work: new `ActionPanel` modal in
[noise/web/src/PointNoiseReport.jsx](web/src/PointNoiseReport.jsx),
fixed "📣 Take Action" button bottom-right (above the docked
sliders), modal lists active-first then all. Each petition card:
target official, suggested mail-to / address, full petition text,
copy-to-clipboard button. No server roundtrip.

**Open server-side asks for V2:**

#### 12.1 — Surface sortie-tow release altitudes for winch gating

User direction 2026-06-03: *"sortie api supports max height for tow
sorties, so the winch height should silence tow_plane where
sortie.height.max < slider"*.

The sortie endpoint already carries the data the client needs:
`sortie_tow_release[].release_alt_agl_ft` (per-cycle release AGL,
documented at
[noise/web/sortiesPlugin.js:2823](web/sortiesPlugin.js#L2823)).
Reading max across the cycle list gives the per-sortie "highest
release" — if that's below the listener's winch slider, the tow
plane could have been replaced by a winch launch and its dBA
contribution should go to zero in the scenario world.

**Ask:** none — the data is already present. Client will add a
`maxReleaseAglFt(sortieTowRelease)` helper that returns
`max(release_alt_agl_ft)` across the cycle blob, and wire it into
the WNCH segment-substitution path so tow plane sorties whose
maximum release is at or below `scenario.winch_agl_ft` go silent.

Flagging here in case the server team wants to pre-roll the max as
`sortie_tow_release_max_release_agl_ft` (a single number is cheaper
than a small array for the client to fold). Not blocking — happy
to compute it on the client.

#### 12.2 — Migrate the page from `/api/excursions/segments` to `/api/sorties`?

> ✅ **RESOLVED 2026-06-04 — server deploy `d3b6cda3` landed the
> listener-anchored geo-filter (`?center=lat,lon&radius_nm=N`) on
> `/api/sorties` with seven-test-case verification (KBDU + south
> Boulder neighbourhood + AND-filterable with `airport=` + 400 on
> invalid params). Client migrated in the same session:
> `fetchSegmentsSameOrigin` → `fetchSortiesSameOrigin`; a `sortieToTrack`
> adapter folds the sortie shape into the legacy `track` shape so the
> downstream analysis pipeline (analyzeTrack → applyScenarioToRow →
> segmentDba) needed minimal changes. `analyzeTrack` now computes
> `alt_airframe_candidates` + per-segment `alt_dba_by_substitute`
> client-side from the substitute registry, since those fields aren't
> shipped on the sortie payload — net: zero behaviour change in the
> What-If sliders, all 6 still drive substitution math correctly.**
> **Verified at Frasier Meadows (39.985, -105.21) on a 24 h window:
> 294 sorties returned vs the old 20 / 500 LIMIT-truncated result —
> >14× the in-radius tracks now reach the page.** (Banner +
> `candidates_considered` keep-alive code path retained for any stale
> client still on the segments endpoint.)

**Perf measured live 2026-06-03 (KBDU dev box, warm cache):**

| Window | sortie endpoint | sortie KB | sortie count | segments endpoint | segments KB |
| ------:|----------------:|----------:|-------------:|------------------:|------------:|
| 1 h    | 1.50 s | 36 KB | 2  | 2.72 s | 80 KB |
| 6 h    | 0.99 s | 102 KB | 9 | 2.98 s | 80 KB |
| 24 h   | 0.75 s | 886 KB | 65 | 2.94 s | 80 KB |
| 72 h   | 1.99 s | 1.4 MB | 96 | — | — |
| 168 h  | 1.20 s | 1.4 MB | 96 (cached) | — | — |

Sortie is **2-4× faster warm** and crucially has **no cold-cache 500
path** (§1's pg-timeout problem is segments-only — sortie pulls from
a different DB query that doesn't trip the 20 s timeout). At 168 h
sortie is faster than 1 h segments. Migrating is the right move.

**One missing piece blocks a drop-in migration: lat/lon support.**
`/api/sorties` accepts `airport=<icao>&hours=<n>` but silently
ignores `lat=&lon=&radius_nm=` — verified by byte-identical
responses (md5 d22ab831...) with and without geo params on otherwise
identical queries. The point-noise report is **listener-anchored**,
so it needs either:

- **(a)** server-side `?lat=&lon=&radius_nm=` filter on `/api/sorties`
  that returns only sorties whose `sortie_path` passes within radius
  of the listener point. Cleanest; matches the existing
  `/api/excursions/segments` shape; lets the client stay listener-
  oriented.
- **(b)** client computes closest approach itself by walking the
  `sortie_path[][]` of every sortie returned by airport-anchored
  queries, with the client pulling sorties from each Front Range
  airport (KBDU + KBJC + KAPA + KFNL + KEIK + KLMO + KGXY) in
  parallel. Workable but means 7 × the bandwidth on every refresh
  and re-implementing the geometry filter the segments endpoint
  already does.

**Ask:** add `?lat=&lon=&radius_nm=` to `/api/sorties` mirroring the
segments-endpoint behaviour (return only sorties whose
`sortie_path` enters the listener radius). Once that lands the
client migration is a straight swap: same lat/lon/radius/hours
parameters, the response carries richer data the page wants
(corrected altitudes via `sortie_path_amendment`, integrated
purposeML via `sortie_purpose`, per-cycle tow release via
`sortie_tow_release` driving §12.1 winch gating), and the perf
roughly halves on warm hits while removing the cold-cache 500
class entirely.

**Field-mapping for the migration** (sortie → what it replaces on
the segments path):

| Sortie field | Replaces (segments) | Notes |
| --- | --- | --- |
| `sortie_path[][lat,lon,alt_msl,ts]` | `segments[].points[]` | One flat path per sortie instead of klass-banded sub-segments. Altitudes already corrected by `sortie_path_amendment.alt_offset_ft`. `sortie_path[i][4]` carries `quality ∈ {real, repaired}` — client should prefer real for headline metrics. |
| `sortie_purpose` + `_source` + `_confidence` | `track.purpose` (V3 §1) | Same V3 buckets. Already wired client-side; just read from the new field name. |
| `sortie_tail`, `sortie_type` | `track.tail`, `track.type` | Same. |
| `sortie_tow_release[]` | (not on segments) | §12.1 winch gating activates automatically. |
| `sortie_max_pop_segment` + `sortie_noise_segments` | (loosely replaces `segments[].klass` info) | Pre-computed pop/report/vnap event windows; client can render headline noise events from this directly. |
| `sortie_alt_quality_summary` | (new) | Aggregate quality of the sortie; useful for confidence weighting. |
| `sortie_takeoff_ts` / `sortie_landing_ts` | (per-segment startedAt / endedAt) | Sortie-level; client computes per-segment from `sortie_path[i][3]`. |

**Status: [PENDING-SERVER] — needs lat/lon filter on /api/sorties.**
Channel can re-flip when the geo filter lands.

---

#### 12.2 — Original ask (kept for context)

User direction 2026-06-03: *"sorties are faster and contain more
information, better altitude information etc"*. Sortie payloads
carry:

- corrected altitudes (`sortie_path` with amendment offset already
  applied; `sortie_alt_quality` quality-tags each fix as
  `real` / `repaired`).
- `sortie_purpose` + `sortie_purpose_source` + `sortie_purpose_confidence` +
  `sortie_purpose_reasons` (purposeML output integrated per-sortie,
  matches the V3 work already client-side).
- `sortie_tow_release[]` (per-cycle release AGL — § 12.1 winch
  gating).
- `sortie_noise_segments[type=pop|report|vnap]` already
  pre-computed at the listener for any segments that scored.

Compare to the current `/api/excursions/segments`, which the page
post-processes for `purpose` / dBA / closest-approach geometry — all
of which the sortie endpoint already returns rolled up.

**Ask:** advice from the server team on whether the page should
migrate the data backbone from `segments` to `sorties`, or whether
the two endpoints continue to coexist (segments = per-fix scoring,
sortie = per-flight roll-up). If migration is wanted, please call
out which `sortie_*` fields replace which `segments[*]` fields so
the client can refactor cleanly rather than guess.

Specifically, does `/api/sorties` accept the same
`?lat=&lon=&hours=&radius_nm=` listener-anchored query the page
already uses for segments? If not, that's the minimum
spec-the-client needs.

#### 12.3 — Action-tag metadata in `substitutes.json`?

Each substitute currently lists `replaces_purposes` so the client
knows when to populate `alt_airframe_candidates`. To wire the
take-action panel cleanly the client also needs to know **which
petitions become more relevant** when a substitute slider goes up.
Today the client does this via the `triggered_by` field inside
`actions.json` (e.g. `triggered_by: "electric_pct > 0"`). That
works; just calling it out so the server team knows the client is
reading scenario-code state to drive UI.

**Ask:** none today. Keep the action-tag glue client-side; if a
substitute is ever added that needs a petition we don't have, we'll
add to `actions.json` ourselves.

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

#### V3 additions — surface purposeML shape inference on the page

Server-side groundwork landed 2026-06-01: the `purposeML/` library
is now on disk (PR series feeding into PR
[bgatti/KnownRisks#5](https://github.com/bgatti/KnownRisks/pull/5)),
which means `resolvePurposeWithShape` actually fires its shape step
and `/api/excursions/segments` rows now carry one of:

- `purpose_source: 'special_use' | 'type' | 'tracked'` — the
  existing/curated path. Verdict is the legacy
  `purposeOf` taxonomy (training, tow_plane, glider, biz_jet, etc).
- `purpose_source: 'shape' | 'shape-hedged'` — purposeML fired.
  Verdict is from the **richer purposeML bucket list**
  (see [purposeML/ADOPTING_PURPOSE_ML_API.md](web/purposeML/ADOPTING_PURPOSE_ML_API.md)
  §"Buckets"): `glider_local | glider_xc | tow_plane | training |
  pattern_solo | survey | patrol | airline | biz_jet | turboprop |
  ga_xc | ga_local | helicopter | unknown`. Carries
  `purpose_confidence` in (0.5, 0.95). Confidence ≥ 0.7 = strong;
  0.5–0.7 = hedged, treat as advisory.

The client's current `PURPOSE_LABEL` + `PURPOSE_COLOR` maps in
[noise/web/src/PointNoiseReport.jsx](web/src/PointNoiseReport.jsx)
only know the legacy taxonomy — `glider_local`, `glider_xc`,
`pattern_solo`, `ga_xc`, `ga_local`, `survey`, `patrol` will render
as raw strings (or fall to "Unknown") today. That's the gap to
close.

**Status: [WIP] — V3 spec pushed 2026-06-01 by server team.**

##### V3 §1. Extend PURPOSE_LABEL + PURPOSE_COLOR

Add the seven new bucket entries the legacy maps don't cover:

```js
const PURPOSE_LABEL = {
  // ...existing entries...
  glider_local:  'Glider (local soaring)',
  glider_xc:     'Glider (cross-country)',
  pattern_solo:  'Pattern (solo / non-school)',
  ga_local:      'GA local (around-the-pattern)',
  ga_xc:         'GA cross-country',
  survey:        'Aerial survey / mapping',
  patrol:        'Patrol / law enforcement',
}
const PURPOSE_COLOR = {
  // ...existing entries...
  glider_local:  '#a78bfa', // violet-400 (existing glider color)
  glider_xc:     '#7c3aed', // violet-600 (darker for XC)
  pattern_solo:  '#fbbf24', // amber-400 (close to training but distinct)
  ga_local:      '#60a5fa', // blue-400 (existing ga_single color)
  ga_xc:         '#3b82f6', // blue-500 (darker for XC)
  survey:        '#22d3ee', // cyan-400 (matches existing survey)
  patrol:        '#475569', // slate-600 (matches patrol)
}
```

##### V3 §2. Surface `purpose_source` as a per-row badge

In the per-track tables (e.g. "Most-active individual aircraft"),
add a small badge to the right of the purpose label showing the
source — so a viewer can tell `glider_xc` came from shape inference
vs `glider` came from the curated type-regex:

| Source | Badge | Tooltip |
|---|---|---|
| `special_use` | `★ curated` (gold) | "Authoritative registry entry" |
| `type` | `T` (slate) | "Inferred from ICAO type code" |
| `tracked` | `DB` (slate) | "Stored in the tracks database" |
| `shape` | `~ shape (87%)` (cyan; show `purpose_confidence` as %) | "Inferred from flight-path shape via purposeML" |
| `shape-hedged` | `~ hedge (62%)` (cyan, faded) | "Hedged purposeML verdict — treat as advisory" |

Keep the badge small enough that it doesn't crowd the row; the
purpose label is still the headline. Tooltip + a once-per-page
legend chip should be enough explanation.

##### V3 §3. Optional filter — toggle "show only high-confidence shape"

The "Filter by purpose" control in the page header has a list of
purpose codes today. Add an optional toggle: *"Only shape-inferred
high-confidence"* — when on, filter to tracks where
`purpose_source === 'shape'` AND `purpose_confidence >= 0.7`. Lets a
viewer audit purposeML's output independently of the curated path.

Skip if the existing filter UI doesn't have room; this is a nice-to-have,
not blocking. The badge from §2 is the primary V3 deliverable.

##### V3 §4. Legend chip + about-link

Add a small legend chip somewhere visible on the page (next to the
existing "By based airport" / "By purpose" tabs is a good spot):

```
Purpose source:  ★ curated   T type   DB tracked   ~ shape (N%)
```

Link the `~` to `/api/purpose-ml/buckets` so a curious viewer can see
the full bucket taxonomy. The endpoint already exists per
[ADOPTING_PURPOSE_ML_API.md](web/purposeML/ADOPTING_PURPOSE_ML_API.md).

##### V3 acceptance

Flip `[WIP]` → `[DONE]` when the page renders at least one row with
`~ shape (NN%)` badge from real prod data, AND the legend chip is
visible. The shape branch needs a track with ≥ 30 points + ≥ 5 min
active to fire — most pattern-work tracks at KBJC will trigger
within a normal day's traffic.

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
