"""End-to-end tests for the maneuver detectors using synthetic tracks.

These are 'does the detector see it' tests, not 'is the confidence exactly X'
tests — the goal is to lock in qualitative behaviour and catch regressions if
someone tightens a threshold and breaks an obvious case.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from phase_ml import features, intent, maneuvers
from phase_ml.data_loader import Point
from tests.synthetic import (
    emergency_descent_track,
    inbound_approach_track,
    s_turns_track,
    steep_turn_track,
    straight_track,
)


def _samples_of(points):
    return features.enrich(points)


def test_straight_cruise_has_no_maneuvers() -> None:
    samples = _samples_of(straight_track(duration_s=180.0, speed_kt=120.0))
    detections = maneuvers.detect_all(samples)
    # Should be silent: no steep turn, no S-turn, no emergency descent.
    types = {d.type for d in detections}
    assert "steep_turn" not in types
    assert "s_turns_across_road" not in types
    assert "emergency_descent" not in types


def test_steep_turn_is_detected() -> None:
    # 360° at 50° bank, 100 kt → ~33 s
    samples = _samples_of(steep_turn_track(turn_total_deg=360.0, bank_deg=50.0, speed_kt=100.0))
    detections = maneuvers.detect_steep_turn(samples)
    assert detections, "expected at least one steep_turn detection"
    d = detections[0]
    assert d.confidence > 0.55
    assert "turn" in d.explanation


def test_s_turns_detected() -> None:
    samples = _samples_of(s_turns_track(n_legs=4, leg_turn_deg=180.0, bank_deg=30.0))
    detections = maneuvers.detect_s_turns(samples)
    assert detections, "expected an S-turn detection"
    assert detections[0].confidence > 0.5


def test_emergency_descent_with_spiral() -> None:
    samples = _samples_of(emergency_descent_track(vs_fpm=-2500, duration_s=80, spiral=True))
    detections = maneuvers.detect_emergency_descent(samples)
    assert detections, "expected an emergency_descent detection"
    d = detections[0]
    assert d.evidence["spiraling"] is True
    assert d.evidence["alt_lost_ft"] > 1500
    assert d.confidence > 0.6


def test_inbound_approach_predicts_correct_airport() -> None:
    points = inbound_approach_track(
        airport_lat=40.0394, airport_lon=-105.2258,  # KBDU
        field_elev_ft=5288,
        runway_heading_deg=260.0,
        start_dist_nm=8.0,
        start_alt_msl_ft=8000.0,
        speed_kt=100.0,
    )
    samples = _samples_of(points)
    # Evaluate intent on a 90-second window ENDING 30 s before touchdown,
    # i.e. while still on final at ~1000 ft AGL — that's where 'inbound' is most
    # meaningful (post-touchdown the geometry collapses).
    end_ts = samples[-1].point.ts_unix - 30.0
    end_idx = next(i for i in range(len(samples) - 1, -1, -1) if samples[i].point.ts_unix <= end_ts)
    start_ts = samples[end_idx].point.ts_unix - 90.0
    start_idx = next(i for i, s in enumerate(samples) if s.point.ts_unix >= start_ts)
    window = features.build_window(samples[start_idx:end_idx + 1])
    summary = intent.summarise(window)
    assert summary.top.airport == "KBDU", f"expected KBDU; got {summary.top.airport} (full: {[(s.airport, round(s.probability,2)) for s in summary.all_scores]})"
    # The aircraft is on a straight-in approach aligned with runway 08 (track ~080°).
    assert summary.top.runway in ("26", "08"), f"got runway {summary.top.runway}"
    assert summary.top.probability > 0.4


def test_full_stop_after_taxi_speed_at_airport() -> None:
    """Aircraft descends to KBDU and then is seen at taxi speed — the outcome
    classifier should label this landed_full_stop, NOT touch_and_go."""
    pre = inbound_approach_track(
        airport_lat=40.0394, airport_lon=-105.2258,
        field_elev_ft=5288,
        runway_heading_deg=260.0,
        start_dist_nm=3.0, start_alt_msl_ft=6500.0,
        speed_kt=90.0,
    )
    # Keep only the part above 100 AGL so we go from descending to a touchdown event.
    pre = [p for p in pre if (p.alt_msl_ft - 5288) > 50]
    # Add a synthetic 30-second taxi at GS ~ 10 kt at field elevation.
    last_t = pre[-1].ts_unix
    last_lat, last_lon = pre[-1].lat, pre[-1].lon
    taxi: list[Point] = []
    for i in range(1, 16):       # 30 s of 2-s samples
        # Move 10 kt = 10/3600 nm/s = ~0.005 nm in 2s — tiny lat/lon delta.
        dlat = i * 1e-5
        taxi.append(Point(lat=last_lat + dlat, lon=last_lon,
                          alt_msl_ft=5288 + 5.0,    # field elev
                          ts_unix=last_t + i * 2.0))
    points = pre + taxi
    samples = features.enrich(points)
    dets = maneuvers.detect_touch_and_go(samples)
    landed = [d for d in dets if d.type == "landed_full_stop"]
    assert landed, f"expected landed_full_stop; got {[d.type for d in dets]}"
    assert landed[0].evidence["airport"] == "KBDU"
    assert landed[0].evidence.get("decision_cue") == "sustained_taxi"


def test_full_stop_when_track_ends_at_airport() -> None:
    """Aircraft descends to KBDU and the track simply ends — FULL_STOP."""
    pre = inbound_approach_track(
        airport_lat=40.0394, airport_lon=-105.2258,
        field_elev_ft=5288,
        runway_heading_deg=260.0,
        start_dist_nm=3.0, start_alt_msl_ft=6500.0,
        speed_kt=90.0,
    )
    # Trim to descend-to-touchdown; no post-data.
    pre = [p for p in pre if (p.alt_msl_ft - 5288) >= -10]
    samples = features.enrich(pre)
    dets = maneuvers.detect_touch_and_go(samples)
    landed = [d for d in dets if d.type == "landed_full_stop"]
    assert landed, f"expected landed_full_stop; got {[d.type for d in dets]}"
    assert landed[0].evidence.get("decision_cue") in ("track_ended_at_airport", "long_silence_no_climbout")


def test_implied_touch_and_go_across_adsb_gap() -> None:
    """ADS-B drops out at low altitude; pre-gap descending → post-gap climbing
    at the same airport should fire a touch_and_go detection even with no
    on-ground sample present."""
    # Build a short approach: 4 fixes coming down toward KBDU runway 26.
    pre = inbound_approach_track(
        airport_lat=40.0394, airport_lon=-105.2258,
        field_elev_ft=5288,
        runway_heading_deg=260.0,
        start_dist_nm=3.0, start_alt_msl_ft=6500.0,
        speed_kt=90.0,
    )
    # Trim to the descent portion; ensure last fix is at ~500 AGL descending.
    # The inbound_approach_track ends at the threshold (~field elev); take the
    # leading portion so the last pre-gap fix is still descending at ~500 AGL.
    pre = [p for p in pre if (p.alt_msl_ft - 5288) > 400][:60]
    t_gap_start = pre[-1].ts_unix
    # 90-second ADS-B blackout
    t_gap_end = t_gap_start + 90.0
    # Post-gap: climbing out from same airport.
    post = []
    for i in range(15):
        t = t_gap_end + i * 2.0
        # Climbing east at 800 fpm, ~300 AGL on emergence rising
        alt = 5288 + 300 + i * 25
        lat = 40.0394 + i * 0.0003
        lon = -105.2258 + i * 0.0003
        post.append(Point(lat=lat, lon=lon, alt_msl_ft=alt, ts_unix=t))
    points = pre + post
    samples = features.enrich(points)
    dets = maneuvers.detect_touch_and_go(samples)
    implied = [d for d in dets if d.evidence.get("implied")]
    assert implied, f"expected an implied touch_and_go; got {dets}"
    assert implied[0].evidence["airport"] == "KBDU"
    assert implied[0].confidence > 0.5


def test_straight_cruise_intent_is_transit() -> None:
    # Cruise eastbound 50 nm from any airport
    samples = _samples_of(straight_track(lat0=40.5, lon0=-103.5, alt_ft=10000.0,
                                         heading_deg=90.0, speed_kt=180.0, duration_s=180.0))
    summary = intent.summarise(samples)
    # Could be 'transit' or some airport with low confidence — but the top
    # hypothesis should not be very confident.
    assert summary.confidence_gap < 0.6 or summary.top.airport is None


if __name__ == "__main__":
    import inspect
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except AssertionError as e:
                print(f"  FAIL {name}: {e}")
                failures += 1
            except Exception as e:
                print(f"  ERROR {name}: {type(e).__name__}: {e}")
                failures += 1
    print(f"\n{failures} failure(s)")
    sys.exit(failures)
