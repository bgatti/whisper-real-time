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
| ACS task identification | ✅ v0.3 live | `acsML/identifier.js` + `/api/acs-ml/identify` endpoint — covers III.B / IV.A,B,E,F,K / V.A,B,C,D / VII.A,B,C / VIII.E / IX.A,B / XI.A |
| IX.B Emergency Approach (simulated) detector | ✅ v0.3 live | `acsML/features.js` `detectEmergencyApproach` — descent < 800 AGL away from airport with climb-out recovery |
| ACS performance-standard scoring | ✅ v0.2 live | `acsML/scoring.js` (V.A, V.B, V.C, V.D) — `result.scores[]` on each flight |
| Energy-based glide / power detection | ⚠ v0.3 spec only | `E = ½v² + g·h` decay rate; specced in IX.B notes, not yet implemented |
| Throttle estimate per detection (mean + pre-event) | ✅ v0.4 live | `acsML/features.js` uses main API's `estimateThrottle` + `perfForType` — attaches `evidence.meanThrottle` and `evidence.preEventThrottle` to every detection. Drives IX.A `spiraling_and_idle`, IX.B `throttle_idle`, VII.B `pre_throttle_low`, VII.C `pre_throttle_high` selectors |
| FAR 61.57(a) day currency | ✅ v0 live | `acsML/identifier.js` emits per-takeoff / per-landing events |
| FAR 61.57(b) night currency | ✅ v0 live | uses NOAA sunrise/sunset (`acsML/suntimes.js`) at airport lat/lon |
| Sortie counts (landings, full_stop, night) | ✅ v0.2 live | `result.phase_summary` has n_takeoffs / n_landings / n_full_stop / n_touch_and_go / n_night_takeoffs / n_night_landings / n_night_full_stop / n_night_touch_and_go |
| FAR 61.57(c) instrument currency | ❌ deferred | needs approach-profile + runway alignment data |
| Wire ACS into `/api/sorties` row | ✅ v0.5 live | `sortie_acs` field on every sortie with ≥ 30 real points |
| Data-quality contract (quality + sanity gates on library inputs) | ✅ v0.6 live | `inputToPointsWithStats` in both libs + `pointsToPurposeMLShape` defended. HTTP responses carry `input_filter: { kept, dropped: { quality, sanity, malformed } }`. See round-6 notes. |
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

---

## 2026-06-01 18:30 — operator feedback round 2 + v0.2 patch

Feedback:
> steep turns are also right handed.
> I don't see emergency descent
> also circle around a point
> but, this looks great
> submit to the channel with instruction for use. sorties should
> include all ACS codes
> plus counts of landings, landing full stop, and night landings
> kickoff a side task to score this segments again the ACS standards
> for speed and altitude deviation, roundness, squareness (cross
> road at 90)

Assistant actions taken:

1. **Steep turn left vs right** — verified. phaseML's
   `detectSteepTurn` is symmetric (the `sign` is computed off
   `turnRateDps > 0 ? 1 : -1` and the explanation says "left" or
   "right" accordingly). Across the full 2026-04-19 day file
   (2800 flights), 49 steep turns fired: **29 left, 20 right**.
   The demo on N1094F flight 6 only happened to capture one steep
   turn that was a left.

2. **Emergency descent** — also working, but was being grossly
   overcounted: **531 instances** firing because phaseML's
   underlying detector triggers on any sustained 1200+ fpm /
   1200+ ft loss, and routine airline / biz-jet descents into KDEN
   trip that. ACS IX.A specifically requires the 30-45° bank, so I
   added a `selector: "spiraling"` field to the JSON spec and the
   identifier honours it (`evidence.spiraling === true` only).
   After gating: **531 → 82** instances. Example output now:
   `"N878UA vs -1399 fpm avg, lost 3000 ft in 130s, spiraled +130°"`
   — a real spiraling descent.

3. **Turns around a point (V.D)** — already working: 3 instances
   on the full day file. It's just a rare maneuver (training only,
   off-airport, over a fixed ground reference). Example:
   `"~27ced1 orbit at (40.0186, -105.2203), radius ~0.05 nm,
   centre spread 958 ft"`.

4. **All ACS codes in sorties** — `result.tasks_demonstrated[]`
   already lists every fired code. Across the day:
   III.B(652), IV.A(1580), IV.B(538), IV.E(142), IV.F(661),
   IV.K(1636), IX.A(82), V.A(49), V.C(36), V.D(3), VII.A(135),
   VII.B(51). V.B Rectangular Course = 0 — this is **correct**:
   V.B is over a ground reference, not around the runway. Pattern
   work belongs to III.B which IS firing.

5. **Counts** — added to `result.phase_summary`:
   - `n_takeoffs`, `n_landings`, `n_touch_and_go`, `n_full_stop`
   - `n_night_takeoffs`, `n_night_landings`, `n_night_full_stop`,
     `n_night_touch_and_go`
   - Existing `phase_seconds` rollup of phaseML per-sample labels

6. **Performance-standard scoring** — built [acsML/scoring.js](web/acsML/scoring.js).
   For each detected V.A / V.B / V.C / V.D instance, returns:
   ```json
   {
     "code": "V.A", "name": "Steep Turns",
     "ts": 1779200120, "durationS": 39,
     "score": 50, "verdict": "outside_standard",
     "breakdown": [
       { "dimension": "bank", "target": "45°", "observed": "62°",
         "tolerance": 5, "deviation": 17, "met": false, "weight": 2 },
       { "dimension": "altitude_deviation", "target": "±100 ft",
         "observed": "±75 ft", "tolerance": 100, "deviation": 75,
         "met": true, "weight": 2 },
       { "dimension": "airspeed_deviation", "target": "±10 kts",
         "observed": "±10 kts", "met": true, "weight": 1,
         "notes": "groundspeed proxy; TAS unavailable from ADS-B" },
       { "dimension": "turn_amount", "target": "360°", "observed": "281°",
         "tolerance": 10, "deviation": 79, "met": false, "weight": 1 }
     ],
     "reasons": ["bank: 62° vs 45°", "turn_amount: 281° vs 360°"]
   }
   ```
   - **V.A Steep Turns**: bank, altitude, airspeed, turn amount
   - **V.B Rectangular Course**: corner squareness (each 90° ±10°), altitude
   - **V.C S-Turns Across a Road**: altitude range, squareness
     at the reference (perpendicular crossings), leg count
   - **V.D Turns Around a Point**: roundness (rStd/rMean ≤ 0.15),
     altitude range, airspeed range

   Sample real result on N1094F flight 6: V.A scored 50 (overbanked
   at 62°, undershot 360° at 281° — bank and amount NO, alt and speed
   OK). V.C scored 67 on three instances (altitude held, but
   squareness 56–61° spread = pilot weaving). Reads like a real
   student-pilot grade sheet.

   Out-of-scope for v0 scoring (verdict='cannot_score'): IV.B / IV.F
   landing accuracy (need touchdown-point intent), IV.A / IV.E
   takeoff (need rotation speed), VII.A slow flight bank target,
   VII.B/C stall recovery technique.

7. **Instructions for use**: see the **How to use** section just below.

---

## 2026-06-01 18:55 — operator feedback round 3 + IX.B detector

Feedback:
> Emergency descent, if proper finds best glide and looks for field
> to land in
> any descent < 800 feet agl away from airport is a good candidate.
> should be able to detect idle by calculating flight energy (speed,
> vs)

Assistant action:

Added [`detectEmergencyApproach`](web/acsML/features.js) mapped to
ACS **IX.B Emergency Approach and Landing (Simulated)**. v0
heuristic — straight off the operator's spec:

- Sustained descent (VS ≤ -300 fpm, ≥ 60 s)
- Ends below 800 ft AGL **AND** more than 3 nm from any known
  airport
- Followed by a sustained climb-out (VS ≥ +300 fpm for ≥ 30 s
  within 90 s of the low point — i.e. the pilot did NOT actually
  land; they powered up and climbed away)
- Speed range during the descent < 50 kts (no rapid deceleration
  that would indicate a different maneuver)

Real example surfaced on the 2026-04-19 day file:
`"N7206C descent -737 fpm to 427 ft AGL, 9.1 nm from KBJC, gs
range 38 kts, recovered to climb"` — textbook simulated emergency
approach. Across the full day, **45 flights** triggered IX.B (1.6%
of the 2800-flight sample).

**Energy-based refinement (deferred to v0.3):** total mechanical
energy is `E = ½v² + g·h`. For a power-off glide at best-glide
speed, energy decays at the drag-limited rate (~3-5 kts equivalent
altitude per second). When we see energy decay > 6 kts-eq-alt/s
that's a steeper-than-glide descent (spiraling emergency descent,
not a glide). When < 1 kts-eq-alt/s the engine is still producing
power. Implementing as `energy_decay_rate_kts_per_s` in
features.js will tighten IX.B further by distinguishing
power-on shallow descents (e.g. routine approach to KEIK from the
NW) from genuine power-off glides. Spec noted in the IX.B JSON
entry's `notes` field.

The standards JSON now has `phaseml_signals: ["emergency_approach_landing"]`
on IX.B so this fires through the normal identifier path.

The TRUE distinction between an INTENTIONAL simulation and a REAL
emergency cannot be made from track shape alone. Operator can
correlate with radio calls / metar / no-landing-occurred to confirm.

---

## 2026-06-01 19:30 — operator feedback round 4: throttle as a first-class citizen

Feedback:
> main API has added throttle as first class citizen of a flight path
> probably this is helpful for emergency descent etc?

YES — huge. The main API's
[throttleEstimate.js](web/throttleEstimate.js) model produces a
per-fix 0..1 throttle estimate (climb_fraction + level_flight_fraction
against the type's POH-derived
[aircraftPerf.js](web/aircraftPerf.js) table). Wired it into acsML:

### What changed

[features.js](web/acsML/features.js):
- `estimateThrottleSeries(samples, typeCode)` produces a parallel
  throttle series. Engineless types return all-null.
- `extractAcsSignals` now attaches `evidence.meanThrottle` and
  `evidence.preEventThrottle` (5 s pre-event window) to EVERY
  detection. Selectors and gates then read those fields.

[identifier.js](web/acsML/identifier.js):
- New selectors:
  - `spiraling_and_idle` — IX.A Emergency Descent (was just
    `spiraling`). Requires both the turn AND idle throttle.
  - `throttle_idle` — IX.B Emergency Approach. Mean throttle <
    0.4 across the descent.
  - `pre_throttle_low` / `pre_throttle_high` — VII.B Power-Off
    vs VII.C Power-On stall selectors. Was inferring from
    post-break VS; now reads the actual pre-break power.

### Measured impact (full 2026-04-19 day, 2800 flights)

| Code | Before throttle | After throttle | Why |
|---|---|---|---|
| IX.A Emergency Descent | 82 | **6** | Required pre-existing spiraling gate kept airline arrival turns; throttle gate finally eliminates them — only idle spirals (real training maneuvers) survive |
| IX.B Emergency Approach | 45 | 40 | Most candidates already had idle-ish throttle |
| VII.B Power-Off Stalls | 51 | 30 | Now requires pre-event throttle < 0.3 |
| VII.C Power-On Stalls | 0 | **9** | Previously couldn't distinguish — defaulted to Power-Off. Now the throttle > 0.7 cases get the right label |

Example IX.A now: "N6719Z vs -1856 fpm avg, lost 1600 ft in 58s,
spiraled -689°" — clearly a training emergency descent at idle.

### Sortie schema (updated)

Every detection's `evidence` object now carries:
```json
{
  "meanThrottle":      0.18,  // mean over [startIdx, endIdx]
  "preEventThrottle":  0.85   // mean over 5 s before startIdx
}
```

(plus all the existing fields from phaseML's detectors). Engineless
aircraft return `null` for both.

### Where the estimate falls short — honest limitations

The throttle estimate is a MODEL not a measurement (the FAA doesn't
broadcast manifold pressure):

- **No wind**: GS is used as a TAS proxy. A 25-kt headwind makes a
  cruise-throttle aircraft look like it's at 50% throttle.
- **No turbo / boosted compensation** at altitude beyond a flat-line
  proxy in the existing code.
- **High-speed descents on idle**: a pilot trading altitude for
  speed at idle produces a high `level_frac` because the model
  thinks "power required at that speed is high" — could over-read
  throttle. Counterbalanced by the strong negative `climb_frac` but
  the cap at 0 means net throttle reads high.
- **Engineless types**: throttle is null. Selectors default to
  passing-through ("we don't know power") so glider tracks still
  classify normally.

The estimate is good enough for the binary gates above (idle vs
cruise vs full). For fine-grained scoring (e.g., "how stable was
the pilot's power setting in slow flight?"), we'd want
real-instrument data we don't have.

### Side benefit

`evidence.preEventThrottle` is also useful for scoring V.A Steep
Turns — ACS doesn't specify a throttle target but instructors
expect power-on-then-back-to-cruise. We can add that to the V.A
scorer if it's wanted; for v0 we left scoring throttle-blind.

---

## 2026-06-01 20:10 — sortie_acs wired into /api/sorties

Operator: "let's ensure it is available in sorties." Done.

[sortiesPlugin.js](web/sortiesPlugin.js):
- New `getAcsMLIdentify()` lazy loader (mirrors the existing
  `getPurposeMLClassify()`). Missing acsML → `sortie_acs: null`,
  no crash.
- Runs `acsMLIdentifyFn` on the SAME real-only points filter
  purposeML uses. Skips if < 30 real points (acsML's own minimum).
- New `sortie_acs` field on every sortie row:
  ```json
  {
    "tasks_demonstrated": [{ "code", "name", "instances", "evidence" }, ...],
    "scores":             [{ "code", "score", "verdict", "breakdown", "reasons" }, ...],
    "currency_events":    [{ "rule", "kind", "ts", "airport", "night", ... }, ...],
    "phase_summary":      { "n_takeoffs", "n_landings", "n_full_stop",
                            "n_touch_and_go", "n_night_takeoffs",
                            "n_night_landings", "n_night_full_stop",
                            "n_night_touch_and_go", "phase_seconds", "total_active_s" },
    "notes":              [...]
  }
  ```
- Response metadata extended:
  - `sortie_acs_classifier` line announces availability + the
    real-only / ≥ 30 pts rule
  - `sortie_evaluation_rules.guarantees.sortie_acs` documents the
    invariant (computed from real-only points; throttle from
    `sortie_path_throttle` drives the VII.B/VII.C/IX.A/IX.B
    selectors per round-4 spec)

Smoke-tested on N1094F's Sunday 2026-04-19 sortie at KLMO:
```
GET /api/sorties?airport=KLMO&day=2026-04-19&tail=N1094F
→ sortie_count: 1
  sortie_acs:
    phase_summary: { n_takeoffs:1, n_landings:37, n_touch_and_go:36, n_full_stop:1 }
    tasks_demonstrated: III.B, IV.A, IV.B, IV.F ×3, IV.K ×36,
                        V.A, V.C ×2, VII.A
    currency_events: 38
    scores: V.A=50 (outside_standard, bank+amount fail),
            V.C=67 (outside_standard, squareness fail)
```

37 landings on a single training sortie because the operator's
sortie-merge rule (5-min ground threshold) collapses many T&Gs
within the lesson into one sortie. The phase_summary counts surface
the full sub-event story while keeping the per-sortie row clean.

---

## 2026-06-01 20:50 — round 6: data-quality contract for phaseML

Operator: "can we ensure that phaseML is consuming high quality
data?"

Audit found one real risk:

  **The library input adapters (`purposeML/service.js` and
  `acsML/service.js` `inputToPoints`) accepted sortie_path-style
  5-tuples `[lat, lon, alt, ts, quality]` but silently dropped the
  5th slot.** If a future caller passed the raw `sortie_path` array
  (which mixes `quality: "real"` and `quality: "repaired"`),
  bridged/synthesized points would have been fed to the classifier
  unfiltered. The current callers (sortiesPlugin, vite.config.js)
  all filter upstream — but the libraries were not defending
  themselves.

Fixed all three input adapters with two gates:

  - **QUALITY gate** — when a tuple's 5th slot is present, drop
    unless it's `"real"` or `"observed"`. Object inputs with a
    `quality` field are gated the same way. Missing quality field
    means "no information — trust" (raw archive / live record).

  - **SANITY gate** — drop fixes that can't physically be in
    flight:
      lat ∉ [-90, 90]
      lon ∉ [-180, 180]
      altMslFt non-finite, < -1000 ft, or > 65000 ft
      tsUnix non-finite, ≤ 0, or > now + 1 day

Defensive depth: each library checks its own inputs. The classifier
still owns its own min-points / min-duration thresholds (≥30 fixes,
≥5 min active wall-clock) — quality+sanity is a layer below that.

### Diagnostics

`/api/purpose-ml/classify` and `/api/acs-ml/identify` now return
`input_filter` so consumers see when filtering kicked in:

```json
{
  "purpose": "pattern_solo",
  "confidence": 0.8,
  "reasons": [...],
  "input_filter": {
    "kept": 158,
    "dropped": { "quality": 3, "sanity": 0, "malformed": 0 }
  }
}
```

If `kept` ends up < 2 after filtering, the endpoints return `400`
with the dropped counts so the caller knows the input was unusable
rather than silently empty.

For in-process use, `inputToPointsWithStats(raw, opts)` returns
`{ points, dropped }` for the same diagnostics.

### Smoke-tested

```
Input: 7 tuples — 1 quality='repaired' + 3 sanity-violations
                  (lat=999, alt=99999, ts=0)
                  + 3 good (1 four-tuple no-quality, 2 five-tuple real)

inputToPointsWithStats →
  kept: 3
  dropped: { quality:1, sanity:3, malformed:0 }
```

### Default behaviour: quality through phaseML's own gates

phaseML itself defends against time gaps: `MAX_SAMPLE_GAP_S = 120 s`
in [phaseML/features.js](web/phaseML/features.js) marks any
inter-fix gap > 2 min as `isSessionBreak`, and every detector
respects breaks. So even garbage timestamps (which would produce
huge inter-fix dt's) get caught BEFORE feeding maneuver detection.
The new quality+sanity gates layer on top of that, not under.

### Status

| Caller | Quality-filter status |
|---|---|
| `sortiesPlugin.js` purposeML + acsML | ✅ filters `quality === 'real'` before calling |
| `sortiesPlugin.js` throttle estimate | ✅ stores `null` at non-real indices |
| `vite.config.js` resolvePurposeWithShape | ✅ `pointsToPurposeMLShape` gates added |
| `vite.config.js` `classifyOneTrack` for phase string | ✅ builds from raw `pts` (no bridged) |
| `/api/purpose-ml/classify` HTTP | ✅ library adapter gates |
| `/api/acs-ml/identify` HTTP | ✅ library adapter gates |

No silent ingestion of bridged data anywhere.

---

## How to use — one-stop reference

What's live and where:

```
purposeML  ──► resolvePurposeWithShape  ──► /api/flights/current.purpose / .purpose_source
                                            sortie_purpose / sortie_purpose_source
                                            /api/purpose-ml/* (standalone)

acsML      ──► sortiesPlugin            ──► /api/sorties[].sortie_acs
                                            /api/acs-ml/* (standalone)

phaseML    ──► both above               ──► (no public surface — composed)

throttle   ──► sortie_path_throttle[]   ──► /api/sorties[].sortie_path_throttle
              estimate evidence              acsML evidence.meanThrottle / preEventThrottle
```

The integration points are deliberately separate from the standalone
endpoints. **For most consumers the answer is `GET /api/sorties` —
everything's already on the row.**

### Recipe 1 — show what a flight school C172 did today (the main pattern)

```sh
curl 'https://web-app-production-fedf.up.railway.app/api/sorties?airport=KBDU&hours=24'
```

Each `sorties[]` row carries:

| Field | What you get |
|---|---|
| `sortie_purpose` / `sortie_purpose_source` | training / pattern_solo / tow_plane / glider_local / … plus `shape` vs `geometry` provenance |
| `sortie_purpose_confidence` / `sortie_purpose_reasons` | when purposeML fired, the 0..1 score + the audit trail |
| `sortie_path` + `sortie_path_throttle` | parallel arrays — path[i] is `[lat, lon, alt_msl, ts_ms, quality]` and throttle[i] is the 0..1 estimate (null if engineless or no prior real fix) |
| `sortie_acs.phase_summary` | `{ n_takeoffs, n_landings, n_touch_and_go, n_full_stop, n_night_takeoffs, n_night_landings, n_night_full_stop, n_night_touch_and_go, phase_seconds }` |
| `sortie_acs.tasks_demonstrated[]` | every ACS code that fired with evidence trail |
| `sortie_acs.scores[]` | V.A / V.B / V.C / V.D performance-standard verdicts |
| `sortie_acs.currency_events[]` | one entry per takeoff and per landing, tagged `night` per FAR 61.57(b) sunrise/sunset |
| `sortie_max_pop_segment` | the loudest 30 s window (literal slice of sortie_path) |

`sortie_acs` is `null` when acsML is unavailable or the sortie has
< 30 real points. `sortie_path_throttle[i]` is `null` for repaired
points, engineless types, and where no prior real fix exists within
60 s. Both rules are documented under `sortie_evaluation_rules.guarantees`.

### Recipe 2 — drill into ONE flight's ACS detail

```sh
# Take a sortie's sortie_path and POST it back to /api/acs-ml/identify
# for the most detailed output (every evidence field, every score
# dimension, every currency event):

curl -X POST 'https://web-app-production-fedf.up.railway.app/api/acs-ml/identify' \
  -H 'Content-Type: application/json' \
  -d @flight.json   # { "points": [{lat,lon,altMslFt,tsUnix}, ...], "typeCode": "C172", "tail": "N1094F" }
```

The standalone endpoint returns the SAME shape as `sortie_acs` but
fed by your own points (e.g. the on-disk yearly archive via
`/identify-archive` with `t0Seconds`). Useful for backtests or for
running acsML against tracks that aren't going through the sortie
extractor.

### Recipe 3 — purpose alone (no ACS detail)

```sh
curl -X POST 'https://web-app-production-fedf.up.railway.app/api/purpose-ml/classify' \
  -H 'Content-Type: application/json' \
  -d '{"points":[...],"typeCode":"C172","tail":"N1094F"}'
# → { "purpose": "pattern_solo", "confidence": 0.8, "reasons": [...] }
```

Or pull from `/api/flights/current` — every row carries `purpose` +
`purpose_source ∈ {"special_use","type","tracked","shape","fallback"}`.

### Recipe 4 — answer "is this aircraft currency-current?"

The library doesn't run the 90-day window for you — currency is
per-pilot, not per-tail. But you can roll up the events for an
aircraft to estimate:

```js
// Sum currency_events across last 90 days of sorties for one tail
const cutoff = Date.now()/1000 - 90 * 86400
const day  = sorties.flatMap(s => s.sortie_acs?.currency_events || [])
  .filter(e => e.rule === '61.57(a)' && e.kind === 'landing' && e.ts > cutoff)
const night = day.filter(e => e.night && e.full_stop)
console.log(`Day 90-day landings: ${day.length}  (≥3 for §61.57(a))`)
console.log(`Night 90-day full-stop landings: ${night.length}  (≥3 for §61.57(b))`)
```

The library labels each event with the right rule code — your client
just sums.

### Recipe 5 — find practice flights that exceeded ACS standards

```js
const trainingFlights = sorties.filter(s =>
  s.sortie_acs?.scores?.some(sc => sc.verdict === 'outside_standard'))
for (const s of trainingFlights) {
  for (const sc of s.sortie_acs.scores) {
    if (sc.verdict === 'outside_standard') {
      console.log(`${s.sortie_tail} ${sc.code} score=${sc.score}: ${sc.reasons.join('; ')}`)
    }
  }
}
// e.g. N1094F V.A score=50: bank: 62° vs 45°; turn_amount: 281° vs 360°
//      N1094F V.C score=67: squareness_at_reference: 61° spread
```

### From a Node module (no HTTP)

```js
import { classifyOneTrack as purposeClassify } from './purposeML/index.js'
import { identifyOneTrack as acsIdentify }      from './acsML/index.js'

const points = sortiePath
  .filter(p => p[4] === 'real')                              // ← REAL-only invariant
  .map(p => ({ lat: p[0], lon: p[1], altMslFt: p[2], tsUnix: Math.floor(p[3]/1000) }))

const purpose = purposeClassify(points, { typeCode, tail, isSchoolFleet })
const acs     = acsIdentify    (points, { typeCode, tail })
```

Both libraries:
- Take the canonical `{ lat, lon, altMslFt, tsUnix }` point shape
- Return a deterministic JSON object — no side effects, no I/O
- Cost ~5-40 ms per flight depending on track length
- Are optional in the build (`requireOpt` pattern in
  [vite.config.js](web/vite.config.js))

### Standalone endpoints reference

| Method | Path | Returns |
|---|---|---|
| GET | `/api/purpose-ml/health` | `{ ok, version }` |
| GET | `/api/purpose-ml/buckets` | all 14 possible purpose labels |
| POST | `/api/purpose-ml/classify` | `{ purpose, confidence, reasons[] }` |
| POST | `/api/purpose-ml/classify-archive` | same, from 4-tuple + t0Seconds |
| POST | `/api/purpose-ml/extract` | full feature vector only |
| GET | `/api/acs-ml/health` | `{ ok, version }` |
| GET | `/api/acs-ml/standards` | Private Pilot ACS + FAR 61.57 JSON |
| POST | `/api/acs-ml/identify` | `{ tasks_demonstrated, scores, currency_events, phase_summary, notes }` |
| POST | `/api/acs-ml/identify-archive` | same, from 4-tuple + t0Seconds |

All endpoints are CORS-open JSON, no auth, stateless. Each call is a
pure function of its input.

### Reading the audit trails

Every result carries a `reasons[]` (purposeML) or per-task
`evidence[]` (acsML) you can inspect when a verdict surprises you.

- **purposeML reasons**: which rule fired, e.g. `"touch_and_go +
  landed_full_stop = 24 ≥ 3 (phaseML)"`. If wrong, the rule's
  threshold needs tuning.
- **acsML evidence**: which phaseML/acsML detector fired, with
  timestamp, duration, confidence, explanation, and now (v0.4)
  `meanThrottle` + `preEventThrottle`. Distinguishes legitimate IX.A
  emergency descent (spiraling + idle throttle) from an airline
  routine descent (straight + cruise throttle).
- **acsML scores**: each dimension lists target, observed,
  tolerance, deviation, met. If `met: false`, `reasons[]` summarises
  why.

### Versions in flight today

| Library | Version | Surfaces |
|---|---|---|
| phaseML | inherited port | per-sample phases + 12 maneuver detectors |
| purposeML | v0.2 (compose phaseML) | resolvePurposeWithShape + standalone API |
| acsML | v0.5 (sortie-wired) | sortie_acs + standalone API |

`/api/acs-ml/health` and `/api/purpose-ml/health` return the running
version.

---

## Sortie schema (single flight, complete)

```json
{
  "tail": "N1094F",
  "typeCode": "C172",
  "tasks_demonstrated": [
    { "code": "III.B", "name": "Traffic Patterns", "instances": 1,
      "evidence": [{ "type": "phase:pattern", "duration_s": 2956, "confidence": 0.9 }] },
    { "code": "IV.A", "name": "Normal Takeoff and Climb", "instances": 2, "evidence": [...] },
    { "code": "IV.F", "name": "Short-Field Approach and Landing", "instances": 7,
      "evidence": [{ "type": "short_field_landing", "ts": 1779228765,
                     "duration_s": 60, "confidence": 0.65,
                     "explanation": "665 fpm sustained descent into touchdown — suspected short-field technique" }] },
    { "code": "IV.K", "name": "Go-Around/Rejected Landing", "instances": 26, "evidence": [...] },
    { "code": "V.A", "name": "Steep Turns", "instances": 1,
      "evidence": [{ "type": "steep_turn", "ts": 1779228175, "duration_s": 39,
                     "confidence": 0.86,
                     "explanation": "sustained left turn, ~704° in 39s, alt range 200 ft" }] },
    { "code": "V.C", "name": "Ground Reference Maneuvers — S-Turns Across a Road", "instances": 3, "evidence": [...] },
    { "code": "VII.A", "name": "Maneuvering During Slow Flight", "instances": 2, "evidence": [...] },
    { "code": "VII.B", "name": "Power-Off Stalls", "instances": 1, "evidence": [...] }
  ],
  "scores": [
    { "code": "V.A", "name": "Steep Turns", "ts": 1779228175, "durationS": 39,
      "score": 50, "verdict": "outside_standard",
      "breakdown": [
        { "dimension": "bank", "target": "45°", "observed": "62°", "met": false, "weight": 2 },
        { "dimension": "altitude_deviation", "target": "±100 ft", "observed": "±75 ft", "met": true, "weight": 2 },
        { "dimension": "airspeed_deviation", "target": "±10 kts", "observed": "±10 kts", "met": true, "weight": 1 },
        { "dimension": "turn_amount", "target": "360°", "observed": "281°", "met": false, "weight": 1 }
      ],
      "reasons": ["bank: 62° vs 45°", "turn_amount: 281° vs 360°"]
    },
    { "code": "V.C", "name": "S-Turns Across a Road", "score": 67, ... }
  ],
  "currency_events": [
    { "rule": "61.57(a)", "kind": "takeoff", "ts": 1779225021,
      "airport": "KLMO", "lat": 40.165, "lon": -105.16, "night": false },
    { "rule": "61.57(a)", "kind": "landing", "ts": 1779226165,
      "airport": "KLMO", "lat": 40.165, "lon": -105.16, "night": false,
      "full_stop": false, "landing_type": "touch_and_go" }
  ],
  "phase_summary": {
    "total_active_s": 7158,
    "phase_seconds": { "pattern": 2956, "practice_area": 1715, ... },
    "n_takeoffs": 2,
    "n_landings": 26,
    "n_touch_and_go": 26,
    "n_full_stop": 0,
    "n_night_takeoffs": 0,
    "n_night_landings": 0,
    "n_night_full_stop": 0,
    "n_night_touch_and_go": 0
  },
  "notes": []
}
```
