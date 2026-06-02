# kickoff_sorties_test — comm channel for purpose + regulatory flight tagging

This file is the running comm channel between operator and assistant while
building the per-flight tagging that surfaces BOTH:

1. **Purpose** (what the flight was for) — produced by `web/purposeML/`
2. **Regulatory** (which FAR / ACS items the flight elements demonstrate
   or count toward currency) — produced by the new `web/acsML/`

Both layers should be applied and displayed for every flight that has
enough captured points (≥ 30 fixes, ≥ 5 min active wall-clock).

## Status

| Layer | State | Source |
|---|---|---|
| Purpose, per-flight | ✅ live | `purposeML/classifier.js` via `resolvePurposeWithShape` in [vite.config.js](web/vite.config.js) |
| Purpose, per-tail (multi-day rollup + FAA registry) | ⚠ offline only | `purposeML/experiments/deep_dive.mjs` |
| ACS task identification | ✅ v0 live | `acsML/identifier.js` + `/api/acs-ml/identify` endpoint |
| FAR 61.57(a) day currency | ✅ v0 live | `acsML/identifier.js` emits per-takeoff / per-landing events |
| FAR 61.57(b) night currency | ✅ v0 live | uses NOAA sunrise/sunset (`acsML/suntimes.js`) at airport lat/lon |
| FAR 61.57(c) instrument currency | ❌ deferred | needs approach-profile + runway alignment data |
| Wire ACS into `/api/flights/current` row | 🔨 pending | follow-on (see Open work below) |
| Kiosk display of purpose + reg | 🔨 pending | follow-on |

## What I'm building (assistant)

`web/acsML/` — sister library to `purposeML/` and `phaseML/`. Composes
phaseML maneuver detections + adds ACS-specific signatures so each
flight emits a list of:

- **ACS Area of Operation tasks demonstrated** (e.g. V.A Steep Turns,
  IV.A Normal Takeoff, IX.A Emergency Descent, V.D Ground Reference
  Maneuvers — Turns Around a Point)
- **FAR currency events generated**:
  - 61.57(a) takeoff + full-stop landing (passenger currency, 90 days)
  - 61.57(b) night takeoff + full-stop landing to night currency
    (1 hour after sunset → 1 hour before sunrise)
  - 61.57(c) instrument approach event (if we can detect ILS/RNAV
    profiles from track shape) + holding pattern
- **Notes** when an element looks like an ACS task but fails the
  performance standard (e.g. "steep turn observed but bank only 38°
  vs ACS 45° ±5°")

Per-flight output shape (proposed):

```json
{
  "flight_id": "...",
  "purpose": { "purpose": "pattern_solo", "source": "shape", "confidence": 0.80, "reasons": [...] },
  "acs": {
    "tasks_demonstrated": [
      { "code": "IV.A", "name": "Normal Takeoff and Climb", "instances": 4 },
      { "code": "IV.B", "name": "Normal Approach and Landing", "instances": 3 },
      { "code": "IV.E", "name": "Go-Around/Rejected Landing", "instances": 1 },
      { "code": "V.A", "name": "Steep Turns", "instances": 1, "note": "bank 42°, alt held ±90 ft — within standard" }
    ],
    "currency_events": [
      { "rule": "61.57(a)", "kind": "takeoff_and_full_stop", "ts": 1779200120, "night": false },
      { "rule": "61.57(a)", "kind": "takeoff_and_full_stop", "ts": 1779200900, "night": false }
    ],
    "notes": []
  }
}
```

The integration plan: extend `resolvePurposeWithShape` →
`resolveFlightTags` that returns `{ purpose, acs }`, and surface both
on the `/api/flights/current` row plus a new
`/api/acs-ml/{health,standards,identify}` endpoint mirroring
purposeML's API.

## Questions / decisions I need from operator

Please confirm or correct:

1. **Standards scope for v0**: Private Pilot ACS (FAA-S-ACS-6B) is the
   widest applicability. Add Commercial (FAA-S-ACS-7B) + Instrument
   (FAA-S-ACS-8B)? Or PP only for v0?
2. **Currency scope for v0**: FAR 61.57(a) and (b) are detectable from
   track + sun position. 61.57(c) instrument currency needs approach
   profile matching (ILS / LPV / RNAV — non-trivial). Include or defer?
3. **Display surface**: which view should show ACS tags first — the
   `/kiosk/impact` flight cards, the `/api/flights/current` table, or
   a new view? My default: extend `/api/flights/current` rows with
   `acs.tasks_demonstrated` count + a per-flight detail endpoint
   `/api/acs-ml/identify` that returns the full breakdown.
4. **`kickoff_sorties_test.md` location**: you said "at the repo root"
   but I couldn't find the file you created (OneDrive sync lag?). I'm
   writing this one at `noise/kickoff_sorties_test.md`. If you meant
   `whipser_real_time/kickoff_sorties_test.md` (the outer repo) or
   `noise/web/kickoff_sorties_test.md`, let me know and I'll move it.
5. **Sample flights for the demo**: I'll use the daily archive
   (`public/tracks_live_2026-04-*.json`) — known-good targets:
   N1094F (Boulder flight school, 100 T&G), N4632F (KBJC pattern),
   N163CP (Textron demo route), one tow plane, one glider. Confirm or
   substitute.

## Running notes (assistant updates as work progresses)

- **2026-06-01 17:30** — Created this file. Inventoried existing
  phaseML maneuver detectors (touch_and_go, landed_full_stop,
  thermalling, holding_pattern, sightseeing_orbit, steep_turn,
  s_turns_across_road, turn_around_a_point, chandelle, lazy_8,
  slow_flight, stall_recovery, emergency_descent) — these alone
  cover ~80% of the ACS performance maneuvers detectable from track
  alone. The rest (slow flight bank/airspeed compliance, climb
  performance per ACS, instrument approach profiles) need new
  detectors.

- **2026-06-01 17:50** — Built acsML v0 end-to-end. Demo on
  `public/tracks_live_2026-04-19.json` works. Highlights:
  - **N1094F (G&M Aircraft / Boulder flight school) Flight 6**, a
    130-min sortie at KLMO, demonstrated 8 distinct ACS tasks
    across 4 Areas of Operation: III.B Traffic Patterns, IV.A
    Normal Takeoff ×2, IV.F Short-Field Approach (preempts IV.B)
    ×7, IV.K Go-Around (touch-and-go) ×26, V.A Steep Turns ×1
    (left turn, 282°), V.C S-Turns Across a Road ×3, VII.A
    Maneuvering During Slow Flight ×2 (GS 53–55 kt vs cruise
    110), VII.B Power-Off Stalls ×1 (selector picked off pre-VS).
    This is a textbook private-pilot lesson and the model
    identified it.
  - **N163CP (Textron) Flight 2** showed IV.F Short-Field Landing
    cleanly — the preemption logic correctly displaced IV.B.
  - **N1812E (KLMO tow C172) Flight 2** showed V.A Steep Turns
    (319° in 30s) — a tow plane unusual to see doing performance
    maneuvers; probably a check ride.
  - 61.57(a) currency events: emitted per takeoff and per landing,
    flagged night/day at the airport's lat/lon using the new NOAA
    sunrise/sunset calculator. None of the 04-19 flights happened
    at night (all day-flying Sunday).
  - 61.57(b) night-currency: implemented but no night events to
    show in the daytime sample.
  - 61.57(c) instrument currency: deferred to v1 (needs approach
    profile / runway alignment data — non-trivial).
  - Tail label issues from earlier persist (e.g. labeling many of
    these as `flight_school` instead of `owner_proficiency`) but
    that's a purposeML refinement, separate from this work.

- **2026-06-01 17:55** — Wired `acsMLApiPlugin()` into
  [vite.config.js](web/vite.config.js) using the same optional-load
  pattern as purposeML. The endpoints are now live:
  - `GET  /api/acs-ml/health`
  - `GET  /api/acs-ml/standards`
  - `POST /api/acs-ml/identify` { points, typeCode?, tail? }
  - `POST /api/acs-ml/identify-archive` { points, t0Seconds, ... }

  Library loads cleanly (8 named exports: `acsMLApiPlugin`,
  `extractAcsSignals`, `identifyAcsSegments`, `identifyOneTrack`,
  `inputToPoints`, `isAfterCivilDusk`, `isFaaNight`, `sunTimes`).

## Open work (post-v0)

1. **Per-row integration**: extend `resolvePurposeWithShape` to
   `resolveFlightTags(stored, type, tail, points, schoolMap)` that
   returns `{ purpose, acs }` — currently `/api/flights/current` rows
   only carry purpose. The acs object will be `{ tasks_demonstrated[],
   currency_events[], notes[] }` per the schema in the kickoff above.
2. **Kiosk display**: a per-flight badge row showing the top 3 ACS
   codes + any night-currency events. Probably belongs next to the
   "Good Neighbor Score" tier on `/kiosk/impact` cards.
3. **Currency rollup endpoint**: `GET /api/acs-ml/currency?tail=N1094F`
   returns `{ rules: [{ rule: '61.57(a)', day_events_in_90d: N,
   night_full_stop_in_90d: N }] }`. Useful for "this aircraft has 22
   day-currency-eligible legs and 0 night-currency events in the last
   90 days."
4. **Iteration on detectors**: power-on vs power-off stall selector
   currently uses the recovery VS, which is post-break — should look
   at pre-break VS. Will need to add an `evidence.preBreakVsFpm` field
   in phaseML's `detectStallRecovery` and update the selector.
5. **Instrument approach detection** (61.57(c)): needs runway data
   from `noise/web/data/runways.json` + approach segment shape
   matching. Defer.

## What I'm asking for from operator

(Re-asking from the top so the answers don't get lost.)

1. PP only for v0 ACS, or also Commercial + Instrument?
   *Assistant default: PP only.* ← shipped.
2. 61.57(a)(b) only for v0 currency, or also (c)?
   *Assistant default: (a)(b) only.* ← shipped.
3. Display surface for ACS tags — kiosk cards, current flights table,
   new view? *Assistant default: extend `/api/flights/current` rows
   plus dedicated `/api/acs-ml/identify` for full detail.*
4. Confirm the file location for `kickoff_sorties_test.md` — I put
   this at `noise/kickoff_sorties_test.md`. Move if needed.
5. Confirm or substitute the 5 sample tails used in the demo.
