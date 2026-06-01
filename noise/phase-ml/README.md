# phase_ml — aircraft activity classifier from ADS-B trajectories

A geometric, no-ML-required first cut at the project sketched in
[`noise/web/src/PHASE_ML_KICKOFF.md`](../web/src/PHASE_ML_KICKOFF.md). What it does, end-to-end:

1. **Reads** a track from the on-disk ADS-B archive at `C:/tmp/noise_data/tracks_<year>.json`.
2. **Enriches** each fix with locally-computed groundspeed, vertical speed, ground track, and turn rate — so historical and live data look identical to the rest of the pipeline.
3. **Phase-labels** each fix against the 9-class oracle (the rule-based `classifyAircraft` ported from JS), then overlays `landed_full_stop` post-hoc.
4. **Detects maneuvers**: steep turns, S-turns across a road, turn-around-a-point, chandelles, lazy 8s, slow flight, stall recoveries, emergency descents, holding patterns, touch-and-goes, thermalling, sightseeing orbits. Each detection carries a confidence in [0, 1], an explanation, and a structured `evidence` dict.
5. **Predicts intent**: given the trailing 3-minute window, produces a posterior over candidate airports (multi-airport, not just KBDU) plus a transit hypothesis, and names the most-likely runway end.

There is **no neural network and no training set** in this package yet. Everything is hand-crafted geometry — fast, explainable, and easy to argue with. The point of this layer is to (a) act as a strong weak-label generator for the eventual XGBoost model that the kickoff doc calls for, and (b) be a usable classifier on day one for anyone who wants to look at archived flights.

## Quick start

```bash
cd noise/phase-ml
python -X utf8 demo.py --year 2026 --top 1 --min-points 500
# or:
python -X utf8 demo.py --tail N4593Y
python -X utf8 demo.py --type GLID --top 3
python tests/test_geometry.py
python tests/test_maneuvers.py
```

No `pip install` needed — pure standard-library Python 3.10+.

## Layout

```
phase_ml/
├── geometry.py     — haversine, bearing, runway frame, bank-from-turn-rate, orbit radius
├── airports.py     — 8 Front-Range airports with runway thresholds & pattern altitudes
├── data_loader.py  — reads tracks_YYYY.json and normalises to Point/Track records
├── features.py     — `enrich()` (per-sample derivatives) + `build_window()` (3-min roll-up)
├── oracle.py       — port of the rule-based 9-label `classifyAircraft` + landed_full_stop
├── maneuvers.py    — 12 PTS-derived geometric detectors
└── intent.py       — Bayesian multi-airport inbound predictor with named entry corridors

demo.py             — runnable end-to-end demo
tests/              — synthetic-trajectory test suite (geometry + maneuvers + intent)
```

## What is novel here vs the JS classifiers in `noise/web/`

The JS code has *four* phase classifiers (see PHASE_ML_KICKOFF.md §2) and one intent ladder. This package collapses all of that into one consistent data flow with two important additions:

- **PTS maneuver detection.** None of the existing classifiers can tell you "this aircraft just did a stall recovery" or "those alternating turns are S-turns across a road." Each detector here encodes the geometric signature of one named maneuver from the FAA Airman Certification Standards.
- **Multi-airport intent.** The JS intent code is hard-coded to KBDU (with a few neighbours bolted on). The Bayesian scorer here treats every airport as a hypothesis and asks "which one is this aircraft inbound to, given closure, heading alignment, runway alignment, energy state, and standard entry corridor?" — and returns a posterior, not a single guess.

## How the detectors work

Each detector is a pure function `samples -> [Detection]`. The pattern:

1. **Find a candidate window** with a maneuver-specific signature (e.g. sustained high turn rate, or a low-AGL local minimum near an airport).
2. **Compute slice metrics** — accumulated turn, altitude range, gs/vs stats, etc.
3. **Apply rejection rules** that distinguish this maneuver from look-alikes (e.g. a slow_flight detection requires being >2 nm from any airport so we don't catch normal approach finals).
4. **Score confidence** as a weighted blend of how cleanly the signature matched.
5. **Merge adjacent same-type detections** that overlap or near-touch.

The maneuvers and their tell-tale signatures:

| Maneuver | Signature (geometric, not labelled) |
|---|---|
| `steep_turn` | sustained bank ≥45° (via bank-from-turn-rate), level ±200 ft, signed turn ≥270° |
| `s_turns_across_road` | ≥3 alternating turn legs of ≥100° each, level within 250 ft |
| `turn_around_a_point` | continuous ≥360° same-sign turn whose centre-of-curvature stays inside a 1200-ft cluster |
| `chandelle` | 180°±35° turn while climbing >300 fpm and bleeding ≥8 kt of groundspeed |
| `lazy_8` | direction reversal + paired climb/dive hump ≥400 ft, two lobes of ≥120° each |
| `slow_flight` | groundspeed at <55% of type's cruise, level, with peak turn rate >1°/s, >2 nm from any airport |
| `stall_recovery` | low-speed setup → vs break < -1200 fpm within 8 s → recovery to vs > -200 fpm within 15 s |
| `emergency_descent` | sustained vs ≤ -1200 fpm for ≥30 s, ≥1200 ft lost, spiral if signed turn >120° |
| `holding_pattern` | racetrack: 2 turn phases + 1 straight leg in 30-120 s window, near-zero net displacement |
| `touch_and_go` / `landed_full_stop` | landing event at a known field (see below — one detector emits two types) |
| `thermalling` | glider-only: continuous ≥720° same-sign turn with net climb ≥300 ft |
| `sightseeing_orbit` | 0.3–3 nm radius wide circle ≥360°, level, ≥3 nm from any airport |

### Landing events: touch_and_go vs landed_full_stop

`detect_touch_and_go()` is the one detector that returns two different `type` labels — the function finds **landing events** and then classifies each as either a touch-and-go or a full-stop landing. Two design constraints drove this:

1. **ADS-B drops out below ~200 ft AGL.** A real touchdown often produces no on-the-ground sample at all — the last fix you see is at 400 ft on short final, then nothing for 30 s–5 min, then the aircraft re-emerges climbing or rolling. The detector therefore has two branches:
   - **Explicit** — fires on a local AGL minimum < 100 ft within 1 nm of a known field.
   - **Implied** — scans session-break boundaries (gap > 30 s, < 15 min) for the `descending-into-airport → gap → still-at-airport` signature. The explanation field carries the `IMPLIED` prefix so you can tell them apart.
2. **T&G vs FULL_STOP needs the post-event window.** The user-facing label depends on what happens *after* the touchdown:
   - `sustained_taxi` — re-emergence at GS < 25 kt for ≥ 20 s near the field → **landed_full_stop** (the rolling aircraft is unambiguously stopped)
   - `track_ended_at_airport` — track simply ends within 3 min of touchdown → **landed_full_stop**
   - `long_silence_no_climbout` — gap > 5 min with no subsequent climb → **landed_full_stop**
   - `climbout_within_window` — vs ≥ +400 fpm seen within 3 min at the same field → **touch_and_go**
   - `indeterminate` — nothing matches; defaults to **touch_and_go** at low confidence (the kiosk should surface these for human review).

The cue used by the verdict is exposed as `evidence.decision_cue` on every detection.

## How the intent predictor works

For each candidate airport (any in the database within 50 nm of current position), score:

```
P(inbound to airport_i) ∝ closure × heading × runway × energy × corridor × prior
```

- **closure_score** — sigmoid of `(d_dist/dt) / groundspeed`. 100% closure = aircraft pointed straight at the field.
- **heading_score** — Gaussian around `bearing_to_field - track`. Tapered out at <2 nm where the geometry is degenerate.
- **runway_score** — Gaussian around the smaller of `|track - runway_heading|` over both runway ends. Strong only when within 12 nm.
- **energy_score** — does current AGL fit a 3°-glideslope continuation from current distance? Penalises "too high" more than "too low."
- **corridor_score** — explicit checks for straight-in (within 0.7 nm of extended centerline), 45° downwind entry, and the above-pattern-altitude cone. Returns the best-matching corridor's name so you can debug the call.

Results are normalised across all candidate airports plus a transit floor, so the output is a proper probability distribution. The transit floor of 0.10 (configurable) keeps the predictor from being over-confident when no airport hypothesis fits well.

## Real-data example (N265SF, a Cessna 172 trainer, 2026 data)

Running `python demo.py --tail N265SF --year 2026` on a 23-hour aggregated track produces (excerpt):

```
Phase distribution (oracle):
    en_route              22.9%
    pattern               22.7%
    practice_area         18.3%
    inbound               13.7%
    departing              6.0%
    taxiing                2.9%
    landed_full_stop       0.6%

Maneuvers detected: 11
    touch_and_go     KGXY (-33 AGL)   descent -650 → climb-out 524 fpm
    touch_and_go     KGXY (-133 AGL)  descent -441 → climb-out 617 fpm
    slow_flight      GS 59 kt (cruise est 110 kt), 158s, alt range 275 ft
    stall_recovery   GS 55 kt → break vs -1500 fpm → recovered to 344 fpm
    s_turns_across_road  3 alternating legs (-375°, +135°, -183°), level within 150 ft
    sightseeing_orbit    ~0.4 nm radius orbit, 11.7 nm from KGXY
    touch_and_go     KLMO (-154 AGL)  descent -1250 → climb-out 681 fpm
    ...

Intent at last sample:
    BEST: inbound to KBJC runway 30  P=0.52  (gap to #2: 0.06)
    dist=0.3 nm closure=+62%  Δhead=2°  runway_off=0°
```

A trainer, doing multi-airport pattern work + air-work over multiple sessions, correctly identified down to the lap.

## Limits / future work

- The thresholds were calibrated against ~5 real tracks across a couple of aircraft types. Expect 10–20 % false positives on the long tail (especially `lazy_8`, which is hard to distinguish from generic "doing something interesting"). The kickoff doc's XGBoost layer is the right place to fix this — feed the oracle + maneuver detections in as features and train a smoothing layer over them.
- Airport database is hard-coded to 8 Front Range fields. To extend, append to `airports.py` — each new field needs lat/lon/elevation/runway-heading. The threshold positions are computed automatically from the airport reference point and runway length.
- The intent predictor has no per-aircraft *base airport* prior wired up yet — pass `prior_by_airport={'KBDU': 2.0}` to `predict_intent()` to bias one airport. The hook is there; the data is in `noise/web/data/fleet.json`.
- `landed_full_stop` cannot be confirmed mid-stream — by construction it requires looking ≥5 min into the future after the on-ground transition. Live consumers need to use `on_ground` and wait.
- `MAX_SAMPLE_GAP_S = 120` in `features.py` defines what counts as a session break in concatenated archives. Tracks captured continuously have no breaks; the per-tail-per-year archive does.

## Why no neural network yet

Because the geometry is doing real work. Every maneuver above has an explicit pre-image in flight-control inputs, and ADS-B gives us enough resolution to see those signatures with simple math. A neural net would learn the same features and obscure them.

The right time to introduce ML is when the geometric detectors disagree on a real case — *that's* the training signal worth gathering. The current detectors emit per-detection `evidence` dicts so you can build a labelled disagreement corpus without re-walking the trajectories.
