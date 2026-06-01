"""Geometric detectors for in-flight maneuvers.

The detectors operate on a list of `Sample`s (the enriched per-fix series from
`features.enrich`). Each detector slides a *purpose-built* window — different
maneuvers need different look-backs — and returns zero or more `Detection`s
with start/end timestamps, confidence in [0, 1], and an `evidence` dict that
explains *why* the maneuver fired. The evidence is what lets us debug false
positives instead of guessing.

The maneuvers covered are drawn from the FAA Airman Certification Standards
(Private + Commercial Pilot) plus a few real-world signatures that don't appear
in any handbook:

    steep_turn          — sustained >45° bank, level, ≥270° of turn
    s_turns_across_road — alternating left/right ~180° legs at constant altitude
    turn_around_a_point — continuous turn with the centre of curvature pinned
    chandelle           — 180° climbing turn with monotonic climb + decelerating
    lazy_8              — alternating left/right with paired climb/dive humps
    slow_flight         — sustained ground speed well below the type's cruise band
    stall_recovery      — abrupt vs drop after a low-speed pull-up; recovery climb
    emergency_descent   — sustained vs ≤ -1500 fpm > 30 s, often turning (spiral)
    holding_pattern     — racetrack: two ~180° turns separated by ~1-min straight legs
    touch_and_go        — descent to runway, brief on-runway interval, immediate climb out
    thermalling         — glider-style continuous turn with net altitude gain
    sightseeing_orbit   — slow, wide orbit holding altitude (helicopter / scenic)

Every detector is a pure function of `samples` (and a small `cfg` dataclass).
None of them need a network, a database, or wall-clock time. They are unit-
testable with synthetic tracks (see tests/).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Sequence

from . import airports as A
from . import geometry as G
from .features import MAX_SAMPLE_GAP_S, Sample


def _walk_back_within(samples: Sequence[Sample], origin_idx: int, max_s: float) -> int:
    """Walk back from origin_idx until either max_s of wall-clock time elapsed
    or we hit a session-break. Returns the earliest valid index."""
    out = origin_idx
    while out > 0:
        prev_dt = samples[out].point.ts_unix - samples[out - 1].point.ts_unix
        if prev_dt > MAX_SAMPLE_GAP_S or prev_dt <= 0:
            break
        if samples[origin_idx].point.ts_unix - samples[out - 1].point.ts_unix >= max_s:
            break
        out -= 1
    return out


def _walk_forward_within(samples: Sequence[Sample], origin_idx: int, max_s: float) -> int:
    """Walk forward from origin_idx until either max_s elapsed or session-break."""
    out = origin_idx
    n = len(samples)
    while out + 1 < n:
        next_dt = samples[out + 1].point.ts_unix - samples[out].point.ts_unix
        if next_dt > MAX_SAMPLE_GAP_S or next_dt <= 0:
            break
        if samples[out + 1].point.ts_unix - samples[origin_idx].point.ts_unix >= max_s:
            break
        out += 1
    return out

# ---------------------------------------------------------------------------
# Detection record
# ---------------------------------------------------------------------------

@dataclass
class Detection:
    type: str                              # maneuver name
    start_idx: int                          # sample index, inclusive
    end_idx: int                            # sample index, inclusive
    start_ts: float                         # epoch s
    end_ts: float                           # epoch s
    confidence: float                       # 0-1
    explanation: str                        # one-line human summary
    evidence: dict = field(default_factory=dict)

    @property
    def duration_s(self) -> float:
        return self.end_ts - self.start_ts

    def __str__(self) -> str:
        m = int(self.duration_s)
        return f"{self.type:<22s} conf={self.confidence:0.2f} dur={m:>4d}s  {self.explanation}"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _windowed_indices(
    samples: Sequence[Sample],
    target_duration_s: float,
) -> list[tuple[int, int]]:
    """Yield (start, end) index pairs (inclusive) where each window ≥ target seconds.

    Steps forward sample-by-sample but coalesces by the natural sample rate; this
    is the simplest way to handle the 2-second cadence without losing accuracy.
    """
    n = len(samples)
    if n < 2:
        return []
    out: list[tuple[int, int]] = []
    start = 0
    for end in range(1, n):
        while samples[end].point.ts_unix - samples[start].point.ts_unix > target_duration_s * 1.5:
            start += 1
        if samples[end].point.ts_unix - samples[start].point.ts_unix >= target_duration_s:
            out.append((start, end))
    return out


def _slice_metrics(samples: Sequence[Sample], lo: int, hi: int) -> dict:
    """Compute the handful of stats most detectors need from a slice."""
    gs = [s.gs_kts for s in samples[lo:hi + 1]]
    vs = [s.vs_fpm for s in samples[lo:hi + 1]]
    alt = [s.point.alt_msl_ft for s in samples[lo:hi + 1]]
    tr = [s.turn_rate_dps for s in samples[lo:hi + 1]]
    tk = [s.track_deg for s in samples[lo:hi + 1]]
    n = len(gs)
    return {
        "n": n,
        "duration_s": samples[hi].point.ts_unix - samples[lo].point.ts_unix,
        "gs_mean": sum(gs) / n,
        "gs_min": min(gs), "gs_max": max(gs), "gs_std": G.std(gs),
        "vs_mean": sum(vs) / n,
        "vs_min": min(vs), "vs_max": max(vs), "vs_std": G.std(vs),
        "alt_start": alt[0], "alt_end": alt[-1],
        "alt_min": min(alt), "alt_max": max(alt),
        "alt_range": max(alt) - min(alt),
        "alt_std": G.std(alt),
        "abs_turn_total_deg": G.cumulative_abs_turn_deg(tk),
        "signed_turn_total_deg": G.cumulative_signed_turn_deg(tk),
        "turn_rate_mean": sum(tr) / n,
        "turn_rate_abs_mean": sum(abs(x) for x in tr) / n,
        "turn_rate_p90": _percentile([abs(x) for x in tr], 0.90),
        "track_start": tk[0], "track_end": tk[-1],
    }


def _percentile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = q * (len(s) - 1)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)


def _merge_adjacent(detections: list[Detection], gap_s: float = 10.0) -> list[Detection]:
    """Combine same-type detections that touch or overlap."""
    if not detections:
        return []
    detections = sorted(detections, key=lambda d: (d.type, d.start_ts))
    out: list[Detection] = [detections[0]]
    for d in detections[1:]:
        prev = out[-1]
        if d.type == prev.type and d.start_ts - prev.end_ts <= gap_s:
            out[-1] = Detection(
                type=prev.type,
                start_idx=prev.start_idx, end_idx=d.end_idx,
                start_ts=prev.start_ts, end_ts=max(prev.end_ts, d.end_ts),
                confidence=max(prev.confidence, d.confidence),
                explanation=prev.explanation,
                evidence={**prev.evidence, **d.evidence, "merged": True},
            )
        else:
            out.append(d)
    return out


# ---------------------------------------------------------------------------
# 1. Steep turn (PTS)
# ---------------------------------------------------------------------------
# Definition (commercial standard): bank ≥ 50° (private: 45°), altitude held
# within ±100 ft, airspeed within ±10 kt, ≥ 360° turn (private: 360°).
#
# Geometric signature:
#   - sustained |turn_rate| consistent with bank ≥ 45° at current GS
#   - net signed turn over the run ≥ 270° (looser than the PTS check)
#   - altitude range ≤ 150 ft
#   - direction does not reverse during the run

@dataclass
class SteepTurnConfig:
    min_bank_deg: float = 45.0
    min_signed_turn_deg: float = 270.0
    max_alt_range_ft: float = 200.0
    min_duration_s: float = 15.0


def detect_steep_turn(samples: Sequence[Sample], cfg: SteepTurnConfig = SteepTurnConfig()) -> list[Detection]:
    """Find sustained steep-turn runs."""
    n = len(samples)
    if n < 5:
        return []
    out: list[Detection] = []
    i = 0
    while i < n - 1:
        # Look for a sustained run where |bank| ≥ threshold and direction stable.
        bank_now = G.bank_angle_deg_from_turn_rate(samples[i].turn_rate_dps, samples[i].gs_kts)
        if bank_now < cfg.min_bank_deg:
            i += 1
            continue
        sign = 1 if samples[i].turn_rate_dps > 0 else -1
        j = i
        while j + 1 < n:
            nxt = samples[j + 1]
            bank = G.bank_angle_deg_from_turn_rate(nxt.turn_rate_dps, nxt.gs_kts)
            same_dir = (nxt.turn_rate_dps * sign) > 0
            if bank < cfg.min_bank_deg * 0.7 or not same_dir:
                # Allow a brief dip but not a full sign flip.
                break
            j += 1
        if j == i:
            i += 1
            continue
        m = _slice_metrics(samples, i, j)
        if (m["duration_s"] >= cfg.min_duration_s
                and abs(m["signed_turn_total_deg"]) >= cfg.min_signed_turn_deg
                and m["alt_range"] <= cfg.max_alt_range_ft):
            # Confidence builds with how close we are to the textbook signature.
            conf = min(1.0, 0.55
                       + 0.15 * min(1.0, abs(m["signed_turn_total_deg"]) / 540.0)
                       + 0.15 * (1.0 - min(1.0, m["alt_range"] / cfg.max_alt_range_ft))
                       + 0.15 * min(1.0, m["duration_s"] / 45.0))
            direction = "right" if sign > 0 else "left"
            out.append(Detection(
                type="steep_turn",
                start_idx=i, end_idx=j,
                start_ts=samples[i].point.ts_unix, end_ts=samples[j].point.ts_unix,
                confidence=conf,
                explanation=f"sustained {direction} turn, ~{abs(m['signed_turn_total_deg']):.0f}° in {m['duration_s']:.0f}s, alt range {m['alt_range']:.0f} ft",
                evidence={**m, "direction": direction, "max_bank_implied_deg": max(
                    G.bank_angle_deg_from_turn_rate(s.turn_rate_dps, s.gs_kts) for s in samples[i:j + 1]
                )},
            ))
        i = j + 1
    return _merge_adjacent(out)


# ---------------------------------------------------------------------------
# 2. S-turns across a road (PTS, ground-reference maneuver)
# ---------------------------------------------------------------------------
# A pilot doing S-turns flies alternating ~180° turns at constant altitude,
# trying to cross an imaginary line (a road) wings-level each time. The
# signature is alternating turn-rate sign, similar amplitude per leg, level
# altitude, and the centroid lying along an axis.
#
# We detect it as: ≥3 sign reversals of turn_rate, each leg averaging ≥120°
# of turn, alt range ≤200 ft over the whole run.

@dataclass
class SturnConfig:
    min_sign_reversals: int = 2          # 3 legs minimum
    min_per_leg_turn_deg: float = 100.0
    max_alt_range_ft: float = 250.0
    min_duration_s: float = 45.0


def detect_s_turns(samples: Sequence[Sample], cfg: SturnConfig = SturnConfig()) -> list[Detection]:
    n = len(samples)
    if n < 10:
        return []
    # Identify contiguous turn legs (same sign) and accumulate their angular change.
    legs: list[tuple[int, int, float]] = []   # (start, end, signed_turn_deg)
    leg_start = 0
    leg_sign = 0
    leg_total = 0.0
    for i in range(1, n):
        delta = G.signed_heading_change(samples[i - 1].track_deg, samples[i].track_deg)
        cur_sign = 1 if delta > 0.5 else (-1 if delta < -0.5 else leg_sign)
        if cur_sign != 0 and cur_sign != leg_sign:
            # Close the previous leg and open a new one.
            if leg_sign != 0:
                legs.append((leg_start, i - 1, leg_total))
            leg_start = i - 1
            leg_sign = cur_sign
            leg_total = delta
        else:
            leg_total += delta
    if leg_sign != 0:
        legs.append((leg_start, n - 1, leg_total))

    out: list[Detection] = []
    # Slide a 3-leg window and check the alternation pattern.
    for start_leg in range(len(legs) - cfg.min_sign_reversals):
        run = legs[start_leg:start_leg + cfg.min_sign_reversals + 1]
        if not all(abs(L[2]) >= cfg.min_per_leg_turn_deg for L in run):
            continue
        signs = [1 if L[2] > 0 else -1 for L in run]
        # Require strict alternation
        if not all(signs[k] != signs[k - 1] for k in range(1, len(signs))):
            continue
        i0, iN = run[0][0], run[-1][1]
        m = _slice_metrics(samples, i0, iN)
        if m["duration_s"] < cfg.min_duration_s or m["alt_range"] > cfg.max_alt_range_ft:
            continue
        leg_strs = ", ".join(f"{L[2]:+.0f}°" for L in run)
        conf = min(1.0, 0.5
                   + 0.1 * len(run)
                   + 0.2 * (1.0 - min(1.0, m["alt_range"] / cfg.max_alt_range_ft))
                   + 0.1 * min(1.0, m["duration_s"] / 120.0))
        out.append(Detection(
            type="s_turns_across_road",
            start_idx=i0, end_idx=iN,
            start_ts=samples[i0].point.ts_unix, end_ts=samples[iN].point.ts_unix,
            confidence=conf,
            explanation=f"{len(run)} alternating legs ({leg_strs}), level within {m['alt_range']:.0f} ft",
            evidence={**m, "legs": [{"start": L[0], "end": L[1], "signed_deg": L[2]} for L in run]},
        ))
    return _merge_adjacent(out, gap_s=30.0)


# ---------------------------------------------------------------------------
# 3. Turn around a point (PTS)
# ---------------------------------------------------------------------------
# The pilot orbits a fixed point on the ground, varying bank to compensate
# for wind. Signature: ≥360° of continuous turn in one direction, altitude
# held, AND the centre of curvature lies in a tight cluster (i.e. the orbit
# centre is the same point throughout).
#
# We estimate the orbit centre at each sample as: position + perpendicular *
# orbit_radius. Then check the spread of those centres.

@dataclass
class TurnAroundPointConfig:
    min_signed_turn_deg: float = 360.0
    max_alt_range_ft: float = 200.0
    min_duration_s: float = 30.0
    max_centre_spread_nm: float = 0.20    # ~1200 ft — keep the centre point pinned


def detect_turn_around_point(samples: Sequence[Sample], cfg: TurnAroundPointConfig = TurnAroundPointConfig()) -> list[Detection]:
    n = len(samples)
    if n < 10:
        return []
    # Scan for steep-turn-like runs but require centre-clustering instead of bank.
    out: list[Detection] = []
    i = 0
    while i < n - 1:
        if abs(samples[i].turn_rate_dps) < 1.0:
            i += 1
            continue
        sign = 1 if samples[i].turn_rate_dps > 0 else -1
        j = i
        while j + 1 < n:
            nxt = samples[j + 1]
            same_dir = (nxt.turn_rate_dps * sign) > 0
            if not same_dir or abs(nxt.turn_rate_dps) < 0.5:
                break
            j += 1
        if j - i < 5:
            i += 1
            continue
        m = _slice_metrics(samples, i, j)
        if (m["duration_s"] < cfg.min_duration_s
                or abs(m["signed_turn_total_deg"]) < cfg.min_signed_turn_deg
                or m["alt_range"] > cfg.max_alt_range_ft):
            i = j + 1
            continue
        # Estimate orbit centre at each sample of the run.
        centres = _estimate_orbit_centres(samples, i, j)
        if not centres:
            i = j + 1
            continue
        cx_lat, cx_lon = G.centroid_latlon(centres)
        spread = max(G.haversine_nm(c[0], c[1], cx_lat, cx_lon) for c in centres)
        if spread > cfg.max_centre_spread_nm:
            i = j + 1
            continue
        radius = sum(G.haversine_nm(samples[k].point.lat, samples[k].point.lon, cx_lat, cx_lon)
                     for k in range(i, j + 1)) / (j - i + 1)
        conf = min(1.0, 0.55
                   + 0.2 * (1.0 - min(1.0, spread / cfg.max_centre_spread_nm))
                   + 0.15 * min(1.0, abs(m["signed_turn_total_deg"]) / 540.0)
                   + 0.10 * (1.0 - min(1.0, m["alt_range"] / cfg.max_alt_range_ft)))
        out.append(Detection(
            type="turn_around_a_point",
            start_idx=i, end_idx=j,
            start_ts=samples[i].point.ts_unix, end_ts=samples[j].point.ts_unix,
            confidence=conf,
            explanation=f"orbit at ({cx_lat:.4f}, {cx_lon:.4f}), radius ~{radius:.2f} nm, centre spread {spread*6076:.0f} ft",
            evidence={**m, "centre_lat": cx_lat, "centre_lon": cx_lon, "radius_nm": radius, "centre_spread_nm": spread},
        ))
        i = j + 1
    return _merge_adjacent(out)


def _estimate_orbit_centres(samples: Sequence[Sample], lo: int, hi: int) -> list[tuple[float, float]]:
    """For each sample in [lo, hi], project the orbit centre perpendicular to track."""
    out: list[tuple[float, float]] = []
    for k in range(lo, hi + 1):
        s = samples[k]
        if abs(s.turn_rate_dps) < 0.5 or s.gs_kts < 5:
            continue
        radius_nm = G.orbit_radius_nm(s.turn_rate_dps, s.gs_kts)
        if not math.isfinite(radius_nm) or radius_nm > 5.0:
            continue
        # Perpendicular bearing: +90° to current track for a right turn, -90° for left.
        perp = s.track_deg + (90.0 if s.turn_rate_dps > 0 else -90.0)
        perp_rad = perp * G.DEG_TO_RAD
        # Walk radius_nm along that bearing from the sample's position.
        # Use the local flat-earth projection.
        x_off = math.sin(perp_rad) * radius_nm
        y_off = math.cos(perp_rad) * radius_nm
        lat0, lon0 = s.point.lat, s.point.lon
        clat, clon = G.xy_nm_to_lat_lon(x_off, y_off, lat0, lon0)
        out.append((clat, clon))
    return out


# ---------------------------------------------------------------------------
# 4. Chandelle (commercial PTS)
# ---------------------------------------------------------------------------
# A 180° climbing turn ending in a stall-warning regime. Signature:
#   - sustained climb (vs > +300 fpm) throughout
#   - 180° ± 30° of signed turn
#   - groundspeed decreases monotonically (loss of KE → PE)
#   - duration 30-90 s

@dataclass
class ChandelleConfig:
    target_turn_deg: float = 180.0
    turn_tolerance_deg: float = 35.0
    min_climb_fpm: float = 300.0
    min_duration_s: float = 25.0
    max_duration_s: float = 120.0
    min_gs_drop_kts: float = 8.0


def detect_chandelle(samples: Sequence[Sample], cfg: ChandelleConfig = ChandelleConfig()) -> list[Detection]:
    n = len(samples)
    out: list[Detection] = []
    for lo, hi in _windowed_indices(samples, cfg.min_duration_s):
        m = _slice_metrics(samples, lo, hi)
        if m["duration_s"] > cfg.max_duration_s:
            continue
        if abs(m["signed_turn_total_deg"] - 180.0) > cfg.turn_tolerance_deg and abs(m["signed_turn_total_deg"] + 180.0) > cfg.turn_tolerance_deg:
            continue
        if m["vs_mean"] < cfg.min_climb_fpm:
            continue
        gs_drop = samples[lo].gs_kts - samples[hi].gs_kts
        if gs_drop < cfg.min_gs_drop_kts:
            continue
        # Climb must be largely monotonic — at least 70% of dt with vs > 0.
        climb_frac = sum(1 for s in samples[lo:hi + 1] if s.vs_fpm > 100) / (hi - lo + 1)
        if climb_frac < 0.65:
            continue
        conf = min(1.0, 0.45
                   + 0.20 * climb_frac
                   + 0.20 * (1.0 - min(1.0, abs(abs(m["signed_turn_total_deg"]) - 180.0) / cfg.turn_tolerance_deg))
                   + 0.15 * min(1.0, gs_drop / 30.0))
        out.append(Detection(
            type="chandelle",
            start_idx=lo, end_idx=hi,
            start_ts=samples[lo].point.ts_unix, end_ts=samples[hi].point.ts_unix,
            confidence=conf,
            explanation=f"180°-ish climbing turn ({m['signed_turn_total_deg']:+.0f}°), climbed {m['alt_end']-m['alt_start']:.0f} ft, lost {gs_drop:.0f} kt",
            evidence={**m, "gs_drop_kts": gs_drop, "climb_fraction": climb_frac},
        ))
    return _merge_adjacent(out)


# ---------------------------------------------------------------------------
# 5. Lazy 8 (commercial PTS)
# ---------------------------------------------------------------------------
# Two 180° climbing/diving turns in opposite directions, joined symmetrically.
# Looks like an "8" on its side. Signature:
#   - direction reverses at least once
#   - paired climb hump + dive hump in altitude
#   - net heading change roughly back to original (360° net = 0 mod 360)
#   - duration 40-120 s for a single lobe; 90-200 s for the full pair

@dataclass
class Lazy8Config:
    # A proper lazy-8 swings ≥400 ft of altitude on each lobe and the turn
    # reverses direction at least once. The lobe geometry should be roughly
    # symmetric: each turn-direction segment accumulates ≥120° of heading.
    min_alt_swing_ft: float = 400.0
    min_duration_s: float = 60.0
    max_duration_s: float = 180.0
    min_sign_reversals: int = 1
    min_per_lobe_turn_deg: float = 120.0


def detect_lazy_8(samples: Sequence[Sample], cfg: Lazy8Config = Lazy8Config()) -> list[Detection]:
    out: list[Detection] = []
    for lo, hi in _windowed_indices(samples, cfg.min_duration_s):
        m = _slice_metrics(samples, lo, hi)
        if m["duration_s"] > cfg.max_duration_s or m["alt_range"] < cfg.min_alt_swing_ft:
            continue
        reversals = _count_sign_reversals_local([s.turn_rate_dps for s in samples[lo:hi + 1]], threshold=2.0)
        if reversals < cfg.min_sign_reversals:
            continue
        # Verify both lobes accumulate meaningful turn.
        lobes = _split_into_turn_lobes(samples, lo, hi)
        if len(lobes) < 2 or not all(abs(L["signed_deg"]) >= cfg.min_per_lobe_turn_deg for L in lobes[:2]):
            continue
        alts = [s.point.alt_msl_ft for s in samples[lo:hi + 1]]
        if not _has_climb_then_dive(alts, min_swing=cfg.min_alt_swing_ft):
            continue
        conf = min(1.0, 0.40
                   + 0.15 * min(1.0, reversals / 2.0)
                   + 0.20 * min(1.0, m["alt_range"] / 800.0)
                   + 0.15 * min(1.0, min(abs(L["signed_deg"]) for L in lobes[:2]) / 180.0))
        out.append(Detection(
            type="lazy_8",
            start_idx=lo, end_idx=hi,
            start_ts=samples[lo].point.ts_unix, end_ts=samples[hi].point.ts_unix,
            confidence=conf,
            explanation=f"{reversals} reversal(s), {m['alt_range']:.0f} ft swing, "
                        f"lobes {abs(lobes[0]['signed_deg']):.0f}°/{abs(lobes[1]['signed_deg']):.0f}°",
            evidence={**m, "reversals": reversals, "lobes": lobes[:4]},
        ))
    return _merge_adjacent(out, gap_s=20.0)


def _split_into_turn_lobes(samples: Sequence[Sample], lo: int, hi: int) -> list[dict]:
    """Split a slice into same-sign turn segments, ignoring brief noise."""
    out: list[dict] = []
    sign = 0
    seg_start = lo
    seg_total = 0.0
    for i in range(lo + 1, hi + 1):
        delta = G.signed_heading_change(samples[i - 1].track_deg, samples[i].track_deg)
        new_sign = 1 if delta > 0.5 else (-1 if delta < -0.5 else sign)
        if new_sign != 0 and new_sign != sign and sign != 0:
            out.append({"start": seg_start, "end": i - 1, "signed_deg": seg_total})
            seg_start = i - 1
            seg_total = delta
        else:
            seg_total += delta
        if new_sign != 0:
            sign = new_sign
    out.append({"start": seg_start, "end": hi, "signed_deg": seg_total})
    return out


def _count_sign_reversals_local(values: Sequence[float], threshold: float) -> int:
    sign = 0
    count = 0
    for v in values:
        if v > threshold:
            new = 1
        elif v < -threshold:
            new = -1
        else:
            continue
        if sign != 0 and new != sign:
            count += 1
        sign = new
    return count


def _has_climb_then_dive(alt: Sequence[float], *, min_swing: float) -> bool:
    """True iff the series rises to a peak then falls (or vice versa) with ≥ min_swing range."""
    if len(alt) < 5:
        return False
    peak_idx = max(range(len(alt)), key=lambda i: alt[i])
    trough_idx = min(range(len(alt)), key=lambda i: alt[i])
    rise = alt[peak_idx] - alt[0]
    fall = alt[peak_idx] - alt[-1]
    if rise >= min_swing / 2 and fall >= min_swing / 2:
        return True
    rise2 = alt[trough_idx] - alt[0]
    fall2 = alt[trough_idx] - alt[-1]
    if -rise2 >= min_swing / 2 and -fall2 >= min_swing / 2:
        return True
    return False


# ---------------------------------------------------------------------------
# 6. Slow flight (PTS)
# ---------------------------------------------------------------------------
# Sustained groundspeed at or below MCA (min controllable airspeed) for type.
# We don't have airspeed, only groundspeed; but for a 3-min window the wind
# averages out enough to be a usable proxy.
#
# Per ICAO type code we have nominal cruise; "slow flight" is GS < 55 % of
# cruise. We also require:
#   - altitude held within ±300 ft (this isn't an approach)
#   - the aircraft is in a practice area (>2 nm from a runway)
#   - at least some maneuvering (turn rate > 1°/s peak)

CRUISE_KTS_BY_TYPE = {
    # Single-engine trainers
    "C172": 110, "C150": 90, "C152": 95, "C162": 95, "C170": 100,
    "P28A": 110, "P28R": 130, "P28T": 140,
    "DA40": 130, "DA20": 110,
    "SR20": 140, "SR22": 165,
    "M20J": 150, "M20P": 145,
    # Twin
    "BE58": 180, "PA44": 150, "DA42": 165,
    # Turbine
    "PC12": 250, "TBM7": 280, "TBM8": 290, "TBM9": 320,
    # Jet
    "C25A": 360, "C25B": 380, "C25C": 400, "CL30": 400, "LJ40": 420,
    # Helicopter
    "R44": 100, "R22": 90, "B06": 110,
    # Gliders are special; treat as 50 kt cruise
    "GLID": 50, "AS21": 50, "AS25": 50, "DG30": 55, "DG40": 55,
}
DEFAULT_CRUISE_KTS = 110


@dataclass
class SlowFlightConfig:
    fraction_of_cruise: float = 0.55
    min_duration_s: float = 30.0
    max_alt_range_ft: float = 400.0
    min_dist_from_runway_nm: float = 2.0
    min_turn_rate_peak_dps: float = 1.0


def detect_slow_flight(
    samples: Sequence[Sample],
    type_code: str = "",
    cfg: SlowFlightConfig = SlowFlightConfig(),
) -> list[Detection]:
    cruise = CRUISE_KTS_BY_TYPE.get(type_code.upper(), DEFAULT_CRUISE_KTS)
    threshold = cruise * cfg.fraction_of_cruise
    n = len(samples)
    out: list[Detection] = []
    i = 0
    while i < n:
        if samples[i].gs_kts > threshold:
            i += 1
            continue
        j = i
        while j + 1 < n and samples[j + 1].gs_kts <= threshold * 1.05:
            j += 1
        if j == i:
            i += 1
            continue
        m = _slice_metrics(samples, i, j)
        if m["duration_s"] < cfg.min_duration_s or m["alt_range"] > cfg.max_alt_range_ft:
            i = j + 1
            continue
        # Must be far enough from any airport to not be a normal final.
        mid = samples[(i + j) // 2].point
        nearest, dist = A.nearest_airport(mid.lat, mid.lon)
        if nearest is not None and dist < cfg.min_dist_from_runway_nm:
            i = j + 1
            continue
        if m["turn_rate_p90"] < cfg.min_turn_rate_peak_dps:
            i = j + 1
            continue
        conf = min(1.0, 0.5
                   + 0.20 * min(1.0, m["duration_s"] / 120.0)
                   + 0.15 * (1.0 - min(1.0, m["alt_range"] / cfg.max_alt_range_ft))
                   + 0.15 * min(1.0, (threshold - m["gs_mean"]) / threshold))
        out.append(Detection(
            type="slow_flight",
            start_idx=i, end_idx=j,
            start_ts=samples[i].point.ts_unix, end_ts=samples[j].point.ts_unix,
            confidence=conf,
            explanation=f"GS {m['gs_mean']:.0f} kt (cruise est {cruise} kt), {m['duration_s']:.0f}s, alt range {m['alt_range']:.0f} ft",
            evidence={**m, "cruise_kts_est": cruise, "threshold_kts": threshold,
                      "dist_from_nearest_airport_nm": dist},
        ))
        i = j + 1
    return _merge_adjacent(out)


# ---------------------------------------------------------------------------
# 7. Stall + recovery (PTS)
# ---------------------------------------------------------------------------
# Signature: groundspeed drops to slow-flight regime (signal that we're at
# stall AOA) AND immediately after, vs goes sharply negative (>-1500 fpm),
# AND a recovery phase brings vs back up. Often in a practice area.

@dataclass
class StallRecoveryConfig:
    min_drop_fpm: float = -1200.0    # vs floor during the break
    min_recovery_fpm: float = -200.0  # vs ceiling during recovery
    pre_low_gs_kts: float = 70.0      # before-break GS is in slow-flight band
    pre_window_s: float = 12.0        # how far back we look for the low-speed setup
    break_window_s: float = 8.0       # how long the drop lasts
    recovery_window_s: float = 15.0    # post-drop climb back


def detect_stall_recovery(
    samples: Sequence[Sample],
    type_code: str = "",
    cfg: StallRecoveryConfig = StallRecoveryConfig(),
) -> list[Detection]:
    cruise = CRUISE_KTS_BY_TYPE.get(type_code.upper(), DEFAULT_CRUISE_KTS)
    low_threshold = max(cfg.pre_low_gs_kts, cruise * 0.55)
    n = len(samples)
    out: list[Detection] = []
    for i in range(2, n - 3):
        if samples[i].vs_fpm > cfg.min_drop_fpm:
            continue
        # Pre-window: low groundspeed sustained for pre_window_s
        pre_start = i
        while pre_start > 0 and samples[i].point.ts_unix - samples[pre_start].point.ts_unix < cfg.pre_window_s:
            pre_start -= 1
        if not all(s.gs_kts < low_threshold for s in samples[pre_start:i]):
            continue
        # Break window
        break_end = i
        while break_end + 1 < n and samples[break_end + 1].point.ts_unix - samples[i].point.ts_unix < cfg.break_window_s:
            if samples[break_end + 1].vs_fpm > cfg.min_recovery_fpm:
                break
            break_end += 1
        if break_end == i:
            continue
        # Recovery window
        rec_end = break_end
        while rec_end + 1 < n and samples[rec_end + 1].point.ts_unix - samples[break_end].point.ts_unix < cfg.recovery_window_s:
            rec_end += 1
        if rec_end == break_end:
            continue
        recovered = samples[rec_end].vs_fpm > cfg.min_recovery_fpm
        if not recovered:
            continue
        m = _slice_metrics(samples, pre_start, rec_end)
        conf = min(1.0, 0.55
                   + 0.20 * min(1.0, abs(samples[i].vs_fpm) / 2500.0)
                   + 0.15 * (1.0 - min(1.0, samples[pre_start].gs_kts / low_threshold)))
        out.append(Detection(
            type="stall_recovery",
            start_idx=pre_start, end_idx=rec_end,
            start_ts=samples[pre_start].point.ts_unix, end_ts=samples[rec_end].point.ts_unix,
            confidence=conf,
            explanation=f"setup GS {samples[pre_start].gs_kts:.0f} kt → break vs {samples[i].vs_fpm:.0f} fpm → recovered to {samples[rec_end].vs_fpm:.0f} fpm",
            evidence={**m, "break_vs_fpm": samples[i].vs_fpm,
                      "low_gs_threshold_kts": low_threshold},
        ))
    # Avoid stacked detections from overlapping pre-windows.
    return _merge_adjacent(out, gap_s=20.0)


# ---------------------------------------------------------------------------
# 8. Emergency descent / spiral (commercial PTS)
# ---------------------------------------------------------------------------
# Sustained heavy descent (vs ≤ -1500 fpm) for ≥ 30 s. Often accompanies a
# continuous bank (spiral down to lose altitude over a fixed point). Distinguish
# from a normal arrival descent by:
#   - vs much steeper than -800 fpm
#   - altitude lost ≥ 1500 ft
#   - either steep bank sustained, or no concurrent inbound-to-airport pattern

@dataclass
class EmergencyDescentConfig:
    max_vs_fpm: float = -1200.0
    min_alt_lost_ft: float = 1200.0
    min_duration_s: float = 30.0


def detect_emergency_descent(samples: Sequence[Sample], cfg: EmergencyDescentConfig = EmergencyDescentConfig()) -> list[Detection]:
    n = len(samples)
    out: list[Detection] = []
    i = 0
    while i < n:
        if samples[i].vs_fpm > cfg.max_vs_fpm:
            i += 1
            continue
        j = i
        while j + 1 < n and samples[j + 1].vs_fpm <= cfg.max_vs_fpm * 0.7:  # allow some relaxation
            j += 1
        if j == i:
            i += 1
            continue
        m = _slice_metrics(samples, i, j)
        alt_lost = m["alt_start"] - m["alt_end"]
        if m["duration_s"] < cfg.min_duration_s or alt_lost < cfg.min_alt_lost_ft:
            i = j + 1
            continue
        spiraling = abs(m["signed_turn_total_deg"]) > 120.0
        conf = min(1.0, 0.50
                   + 0.20 * min(1.0, alt_lost / 4000.0)
                   + 0.15 * (1.0 if spiraling else 0.0)
                   + 0.15 * min(1.0, m["duration_s"] / 90.0))
        out.append(Detection(
            type="emergency_descent",
            start_idx=i, end_idx=j,
            start_ts=samples[i].point.ts_unix, end_ts=samples[j].point.ts_unix,
            confidence=conf,
            explanation=f"vs {m['vs_mean']:.0f} fpm avg, lost {alt_lost:.0f} ft in {m['duration_s']:.0f}s"
                        + (f", spiraled {m['signed_turn_total_deg']:+.0f}°" if spiraling else ""),
            evidence={**m, "alt_lost_ft": alt_lost, "spiraling": spiraling},
        ))
        i = j + 1
    return _merge_adjacent(out)


# ---------------------------------------------------------------------------
# 9. Holding pattern (IFR)
# ---------------------------------------------------------------------------
# Standard hold: 1-min legs + 180° turns. Signature: track sequence
# straight–180°–straight–180°, with the two straights anti-parallel and
# altitude held. Detect as two 180° turns within ~3 min, separated by a
# straight segment of ~1 nm, at the same altitude.

@dataclass
class HoldingConfig:
    leg_min_s: float = 30.0
    leg_max_s: float = 120.0
    turn_target_deg: float = 180.0
    turn_tolerance_deg: float = 35.0
    max_alt_range_ft: float = 200.0
    min_duration_s: float = 180.0
    max_duration_s: float = 600.0
    # A holding pattern is exactly two 180° turns + two straights; allow some
    # noise but not 25 "turns".
    max_turn_phases: int = 6
    turn_phase_threshold_dps: float = 3.0    # standard-rate turn or steeper


def detect_holding_pattern(samples: Sequence[Sample], cfg: HoldingConfig = HoldingConfig()) -> list[Detection]:
    n = len(samples)
    out: list[Detection] = []
    for lo, hi in _windowed_indices(samples, cfg.min_duration_s):
        m = _slice_metrics(samples, lo, hi)
        if m["duration_s"] > cfg.max_duration_s or m["alt_range"] > cfg.max_alt_range_ft:
            continue
        # Roughly: net turn close to ±360° over the window, and same sign throughout
        signed = m["signed_turn_total_deg"]
        if not (300 <= abs(signed) <= 420):
            continue
        # Identify two turn segments with two straight segments between them.
        # Use a higher threshold so noisy ADS-B doesn't fragment a smooth turn
        # into dozens of tiny phases.
        phases = _classify_phases([s.turn_rate_dps for s in samples[lo:hi + 1]],
                                  threshold=cfg.turn_phase_threshold_dps)
        n_turns = sum(1 for p in phases if p["kind"] == "turn")
        n_straights = sum(1 for p in phases if p["kind"] == "straight")
        if n_turns < 2 or n_turns > cfg.max_turn_phases or n_straights < 1:
            continue
        # Straight legs must be in the right time band
        straight_durations = []
        for p in phases:
            if p["kind"] == "straight":
                dt = samples[lo + p["end"]].point.ts_unix - samples[lo + p["start"]].point.ts_unix
                straight_durations.append(dt)
        if not straight_durations or not any(cfg.leg_min_s <= d <= cfg.leg_max_s for d in straight_durations):
            continue
        conf = min(1.0, 0.45
                   + 0.20 * min(1.0, n_turns / 4.0)
                   + 0.20 * (1.0 - min(1.0, m["alt_range"] / cfg.max_alt_range_ft))
                   + 0.15 * min(1.0, m["duration_s"] / 360.0))
        out.append(Detection(
            type="holding_pattern",
            start_idx=lo, end_idx=hi,
            start_ts=samples[lo].point.ts_unix, end_ts=samples[hi].point.ts_unix,
            confidence=conf,
            explanation=f"{n_turns} 180° turn(s) + {n_straights} leg(s), signed turn {signed:+.0f}°",
            evidence={**m, "n_turns": n_turns, "n_straights": n_straights,
                      "straight_durations_s": straight_durations},
        ))
    return _merge_adjacent(out, gap_s=120.0)


def _classify_phases(turn_rate: Sequence[float], threshold: float) -> list[dict]:
    """Return [{kind: 'turn' | 'straight', start, end}, ...]."""
    out = []
    if not turn_rate:
        return out
    cur_kind = "turn" if abs(turn_rate[0]) > threshold else "straight"
    start = 0
    for i in range(1, len(turn_rate)):
        kind = "turn" if abs(turn_rate[i]) > threshold else "straight"
        if kind != cur_kind:
            out.append({"kind": cur_kind, "start": start, "end": i - 1})
            start = i
            cur_kind = kind
    out.append({"kind": cur_kind, "start": start, "end": len(turn_rate) - 1})
    return out


# ---------------------------------------------------------------------------
# 10. Landing events  (touch_and_go vs landed_full_stop)
# ---------------------------------------------------------------------------
# A landing event is any descent-to-touchdown at a known airport. After the
# touchdown we look at the *outcome* to label the event:
#
#   touch_and_go      — quick climb-out (vs ≥ +400 fpm) within ~2 min
#   landed_full_stop  — any of:
#                         · sustained taxi speed (GS < 25 kt) within ~3 min
#                         · long ADS-B silence (> 5 min) without a climb-out
#                         · the track simply ends within ~3 min of touchdown
#
# Both cases produce one Detection — the `.type` field carries the verdict
# ("touch_and_go" or "landed_full_stop"). They are exposed under one detector
# function for simplicity of integration.
#
# ADS-B reception drops out routinely below ~200 ft AGL; the *implied* branch
# below scans session-break boundaries for the descend-into-airport →
# climb-from-airport pattern, so we still catch touchdowns we never saw.

@dataclass
class TouchAndGoConfig:
    max_touchdown_agl_ft: float = 100.0
    max_touchdown_dist_nm: float = 1.0
    min_descent_before_fpm: float = -200.0
    min_climb_after_fpm: float = 400.0
    touchdown_window_s: float = 20.0
    surrounding_window_s: float = 60.0
    # ----- implied-touchdown (ADS-B dropout) branch -----
    # ADS-B drops out near the ground; a real touchdown often has *no* low-AGL
    # sample. The implied branch scans session breaks and asks "did we arrive
    # at airport X descending, and re-emerge at airport X climbing?"
    implied_max_pre_agl_ft: float = 800.0
    implied_max_post_agl_ft: float = 1500.0
    implied_max_dist_nm: float = 2.0
    implied_min_gap_s: float = 30.0
    implied_max_gap_s: float = 15 * 60.0    # > 15 min looks more like two flights
    implied_min_pre_descent_fpm: float = -200.0
    implied_min_post_climb_fpm: float = 200.0
    # ----- T&G vs FULL_STOP classifier -----
    # Window of wall-clock time we scan after touchdown for the outcome.
    outcome_window_s: float = 180.0
    # Below this GS the aircraft is plausibly rolling / taxiing.
    taxi_gs_kts: float = 25.0
    # Sustained taxi for at least this long → FULL_STOP. A T&G aircraft would
    # be back at flying speed within ~10 s of touchdown; 20 s of sustained
    # taxi is unambiguous evidence of a stop.
    taxi_sustained_s: float = 20.0
    # A post-touchdown silence longer than this with no climb-out → FULL_STOP.
    silence_then_full_stop_s: float = 5 * 60.0


def _classify_touchdown_outcome(
    samples: Sequence[Sample],
    touchdown_idx: int,
    airport: A.Airport,
    cfg: TouchAndGoConfig,
) -> tuple[str, float, dict]:
    """Look forward from the touchdown and decide T&G vs FULL_STOP.

    Returns (type_label, confidence, evidence). The two cues the user
    identified:

      1. We get on-ground groundspeed: post-event GS < taxi_gs_kts sustained
         for ≥ taxi_sustained_s while still near the airport → FULL_STOP.
      2. We just don't get data for N seconds:
            - if a climb-out emerges within outcome_window_s     → T&G
            - if no climb-out and the silence is long             → FULL_STOP
            - if the track simply ends near the airport           → FULL_STOP
    """
    n = len(samples)
    s0 = samples[touchdown_idx]
    deadline = s0.point.ts_unix + cfg.outcome_window_s

    sustained_taxi_s = 0.0
    last_ts = s0.point.ts_unix
    last_idx = touchdown_idx
    # None = no post-touchdown sample at the airport has been seen yet.
    # We avoid a numeric sentinel so it can't leak into explanations.
    max_post_vs: float | None = None
    saw_data_at_airport = False
    silence_run_s = 0.0

    for k in range(touchdown_idx + 1, n):
        s = samples[k]
        if s.point.ts_unix > deadline:
            break
        # Inter-sample gap (counts as 'silence').
        dt_step = s.point.ts_unix - samples[k - 1].point.ts_unix
        if dt_step > 0:
            silence_run_s = max(silence_run_s, dt_step) if dt_step > 60 else silence_run_s
        ap, dist = A.nearest_airport(s.point.lat, s.point.lon)
        if ap is None or ap.icao != airport.icao or dist > cfg.implied_max_dist_nm:
            # left the airport area; record whatever we have
            continue
        saw_data_at_airport = True
        agl = s.point.alt_msl_ft - airport.field_elev_ft
        # Cue 1: taxi-speed cluster (must also be at low AGL).
        if s.gs_kts < cfg.taxi_gs_kts and agl < 200:
            sustained_taxi_s += s.dt_s if s.dt_s > 0 else 2.0
        else:
            sustained_taxi_s = 0.0
        # Cue 2: any post-event climb?
        if max_post_vs is None or s.vs_fpm > max_post_vs:
            max_post_vs = s.vs_fpm
        last_ts = s.point.ts_unix
        last_idx = k
        # Early decision: sustained taxi is enough by itself.
        if sustained_taxi_s >= cfg.taxi_sustained_s:
            ev = {
                "decision_cue": "sustained_taxi",
                "taxi_seconds": sustained_taxi_s,
                "max_post_vs_fpm": max_post_vs,
                "airport": airport.icao,
            }
            return "landed_full_stop", 0.85, ev

    no_climbout = max_post_vs is None or max_post_vs < cfg.min_climb_after_fpm

    # Track ended within the window AND we never saw a climb-out → FULL_STOP.
    if last_idx >= n - 1 and no_climbout:
        ev = {
            "decision_cue": "track_ended_at_airport",
            "max_post_vs_fpm": max_post_vs,
            "airport": airport.icao,
        }
        return "landed_full_stop", 0.7, ev

    # Long silence followed by no climb-out → FULL_STOP.
    if silence_run_s >= cfg.silence_then_full_stop_s and no_climbout:
        ev = {
            "decision_cue": "long_silence_no_climbout",
            "silence_s": silence_run_s,
            "max_post_vs_fpm": max_post_vs,
            "airport": airport.icao,
        }
        return "landed_full_stop", 0.75, ev

    # Otherwise: climb-out within the window → T&G.
    if max_post_vs is not None and max_post_vs >= cfg.min_climb_after_fpm:
        ev = {
            "decision_cue": "climbout_within_window",
            "max_post_vs_fpm": max_post_vs,
            "airport": airport.icao,
        }
        return "touch_and_go", 0.8, ev

    # Indeterminate — call it a T&G with low confidence (it's the less harmful
    # default for noise reports; an "incorrect FULL_STOP" prompts a kiosk alert).
    return "touch_and_go", 0.45, {
        "decision_cue": "indeterminate",
        "max_post_vs_fpm": max_post_vs,
        "airport": airport.icao,
    }


def detect_touch_and_go(samples: Sequence[Sample], cfg: TouchAndGoConfig = TouchAndGoConfig()) -> list[Detection]:
    """Fire once per touchdown event by walking through local AGL minima.

    The previous version emitted one candidate per low-altitude sample and
    relied on merging — that fuses 20 pattern laps into one 75-min blob.
    Instead, find each local minimum of AGL-within-an-airport-zone and emit
    one detection per minimum.
    """
    n = len(samples)
    if n < 5:
        return []
    # Compute (airport, AGL, dist) per sample; None where outside any airport zone.
    annot: list[tuple[str | None, float, float, A.Airport | None]] = []
    for s in samples:
        ap, dist = A.nearest_airport(s.point.lat, s.point.lon)
        if ap is None or dist > cfg.max_touchdown_dist_nm:
            annot.append((None, float("inf"), float("inf"), None))
            continue
        agl = s.point.alt_msl_ft - ap.field_elev_ft
        annot.append((ap.icao, agl, dist, ap))

    out: list[Detection] = []
    last_emit_ts = -1e18
    i = 1
    while i < n - 1:
        ap_icao, agl, dist, ap_obj = annot[i]
        if ap_icao is None or agl > cfg.max_touchdown_agl_ft:
            i += 1
            continue
        # Find the local AGL minimum: walk forward while AGL is *strictly*
        # decreasing. Stop as soon as AGL plateaus or rises — otherwise we
        # walk through the post-touchdown taxi and starve the outcome
        # classifier of forward-looking data.
        j = i
        while (j + 1 < n
               and annot[j + 1][0] == ap_icao
               and annot[j + 1][1] < annot[j][1] - 0.5):
            j += 1
        # j is the local minimum within this airport zone
        if samples[j].point.ts_unix - last_emit_ts < cfg.surrounding_window_s:
            i = j + 1
            continue
        s = samples[j]
        agl_min = annot[j][1]
        if agl_min > cfg.max_touchdown_agl_ft:
            i = j + 1
            continue
        # Walk back/forward but stop at session breaks (avoids fusing 75 min
        # of pattern work into one detection).
        pre_lo = _walk_back_within(samples, j, cfg.surrounding_window_s)
        post_hi = _walk_forward_within(samples, j, cfg.surrounding_window_s)
        pre_vs = [x.vs_fpm for x in samples[pre_lo:j]]
        if not pre_vs:
            i = j + 1
            continue
        if min(pre_vs) > cfg.min_descent_before_fpm:
            i = j + 1
            continue
        # Classify the outcome (T&G vs FULL_STOP) by looking forward.
        verdict, verdict_conf, verdict_ev = _classify_touchdown_outcome(
            samples, j, ap_obj, cfg,
        )
        m = _slice_metrics(samples, pre_lo, post_hi)
        # Geometric confidence multiplied by outcome confidence.
        geom_conf = min(1.0, 0.4
                        + 0.20 * min(1.0, abs(min(pre_vs)) / 800.0)
                        + 0.15 * (1.0 - min(1.0, agl_min / cfg.max_touchdown_agl_ft)))
        conf = (geom_conf + verdict_conf) / 2.0
        cue = verdict_ev.get("decision_cue")
        post_vs = verdict_ev.get("max_post_vs_fpm")
        if verdict == "touch_and_go":
            if cue == "climbout_within_window" and post_vs is not None:
                post_summary = f"climb-out {post_vs:.0f} fpm"
            else:
                post_summary = f"indeterminate (no post-touchdown data at {ap_icao})"
        else:
            post_summary = f"FULL_STOP ({cue or '?'})"
        out.append(Detection(
            type=verdict,
            start_idx=pre_lo, end_idx=post_hi,
            start_ts=samples[pre_lo].point.ts_unix, end_ts=samples[post_hi].point.ts_unix,
            confidence=conf,
            explanation=f"touchdown at {ap_icao} ({agl_min:.0f} AGL), descent {min(pre_vs):.0f} → {post_summary}",
            evidence={**m, "airport": ap_icao, "min_agl_ft": agl_min,
                      "pre_min_vs": min(pre_vs), **verdict_ev},
        ))
        last_emit_ts = s.point.ts_unix
        i = post_hi + 1

    # Implied-touchdown pass: scan session breaks for the
    # descending-to-airport → gap → climbing-from-airport signature.
    out.extend(_detect_implied_touchdowns(samples, cfg, suppress_within_s=cfg.surrounding_window_s,
                                          existing=out))
    out.sort(key=lambda d: d.start_ts)
    return out


def _detect_implied_touchdowns(
    samples: Sequence[Sample],
    cfg: TouchAndGoConfig,
    *,
    suppress_within_s: float,
    existing: Sequence[Detection],
) -> list[Detection]:
    """ADS-B dropout-aware touchdown detector.

    Walks the sample list, finds gaps in (`implied_min_gap_s`, `implied_max_gap_s`],
    and asks whether the last fix before the gap was a descent into an airport
    and the first fix after was a climb-out from the same airport.

    Detections that overlap an existing explicit `touch_and_go` are suppressed,
    so we don't double-count when ADS-B *did* see the touchdown.
    """
    n = len(samples)
    if n < 2:
        return []

    def overlaps_existing(ts: float) -> bool:
        for d in existing:
            if abs(d.start_ts - ts) < suppress_within_s or (d.start_ts <= ts <= d.end_ts):
                return True
        return False

    out: list[Detection] = []
    for i in range(1, n):
        gap = samples[i].point.ts_unix - samples[i - 1].point.ts_unix
        if gap <= cfg.implied_min_gap_s or gap > cfg.implied_max_gap_s:
            continue
        pre = samples[i - 1].point
        post = samples[i].point
        # Both sides must be close to the same airport.
        ap_pre, dist_pre = A.nearest_airport(pre.lat, pre.lon)
        ap_post, dist_post = A.nearest_airport(post.lat, post.lon)
        if ap_pre is None or ap_post is None or ap_pre.icao != ap_post.icao:
            continue
        if dist_pre > cfg.implied_max_dist_nm or dist_post > cfg.implied_max_dist_nm:
            continue
        agl_pre = pre.alt_msl_ft - ap_pre.field_elev_ft
        agl_post = post.alt_msl_ft - ap_post.field_elev_ft
        if agl_pre > cfg.implied_max_pre_agl_ft or agl_post > cfg.implied_max_post_agl_ft:
            continue
        # Pre: should be descending — use the LAST few pre-gap samples since
        # the immediate pre-gap fix may be smoothed.
        pre_vs_window = [s.vs_fpm for s in samples[max(0, i - 5):i] if s.dt_s > 0]
        if not pre_vs_window or min(pre_vs_window) > cfg.implied_min_pre_descent_fpm:
            continue
        mid_ts = (pre.ts_unix + post.ts_unix) / 2.0
        if overlaps_existing(mid_ts):
            continue
        # Classify outcome. The "touchdown sample" for the classifier is the
        # FIRST post-gap fix (since that's the earliest data we have after the
        # implied touchdown). The taxi cue is what tells us FULL_STOP vs T&G:
        # if the aircraft re-emerges at taxi speed, it landed and is rolling.
        verdict, verdict_conf, verdict_ev = _classify_touchdown_outcome(
            samples, i, ap_post, cfg,
        )
        # Implied geometry confidence: stronger when both AGLs were low and
        # the pre-gap descent was committed.
        geom_conf = min(1.0, 0.4
                        + 0.10 * (1.0 - min(1.0, agl_pre / cfg.implied_max_pre_agl_ft))
                        + 0.10 * (1.0 - min(1.0, agl_post / cfg.implied_max_post_agl_ft))
                        + 0.15 * min(1.0, abs(samples[i - 1].vs_fpm) / 800.0))
        conf = (geom_conf + verdict_conf) / 2.0
        out.append(Detection(
            type=verdict,
            start_idx=i - 1, end_idx=i,
            start_ts=pre.ts_unix, end_ts=post.ts_unix,
            confidence=conf,
            explanation=(
                f"IMPLIED {verdict.upper()} at {ap_pre.icao}: "
                f"descent {samples[i-1].vs_fpm:.0f} fpm @ {agl_pre:.0f} AGL → "
                f"ADS-B gap {gap:.0f}s → "
                f"cue={verdict_ev.get('decision_cue', '?')}"
            ),
            evidence={
                "airport": ap_pre.icao,
                "gap_s": gap,
                "pre_agl_ft": agl_pre, "post_agl_ft": agl_post,
                "pre_vs_fpm": samples[i - 1].vs_fpm,
                "post_vs_fpm": samples[i].vs_fpm,
                "pre_dist_nm": dist_pre, "post_dist_nm": dist_post,
                "implied": True,
                **verdict_ev,
            },
        ))
    return out
    return out


# ---------------------------------------------------------------------------
# 11. Thermalling (glider)
# ---------------------------------------------------------------------------
# Continuous turn with NET climb under power-off. Diagnostic only when we know
# the aircraft type is a glider (no engine to climb on); otherwise looks like
# a TAP or pattern.

@dataclass
class ThermallingConfig:
    min_signed_turn_deg: float = 720.0
    min_net_climb_ft: float = 300.0
    min_duration_s: float = 120.0
    max_alt_range_for_centre_ft: float = 50000.0  # gliders can climb thousands


GLIDER_TYPES = {"GLID", "AS21", "AS22", "AS25", "AS33", "DG30", "DG40", "VENT", "LS6", "LS8", "G103", "K21"}


def detect_thermalling(samples: Sequence[Sample], type_code: str = "", cfg: ThermallingConfig = ThermallingConfig()) -> list[Detection]:
    if type_code.upper() not in GLIDER_TYPES:
        return []
    n = len(samples)
    out: list[Detection] = []
    i = 0
    while i < n - 1:
        if abs(samples[i].turn_rate_dps) < 1.0:
            i += 1
            continue
        sign = 1 if samples[i].turn_rate_dps > 0 else -1
        j = i
        while j + 1 < n:
            nxt = samples[j + 1]
            if (nxt.turn_rate_dps * sign) <= 0 or abs(nxt.turn_rate_dps) < 0.5:
                # Allow tiny gaps
                gap_end = j + 1
                while gap_end < n and abs(samples[gap_end].turn_rate_dps) < 0.5:
                    gap_end += 1
                if gap_end - j > 3:
                    break
            j += 1
        if j - i < 10:
            i += 1
            continue
        m = _slice_metrics(samples, i, j)
        net_climb = m["alt_end"] - m["alt_start"]
        if (abs(m["signed_turn_total_deg"]) < cfg.min_signed_turn_deg
                or net_climb < cfg.min_net_climb_ft
                or m["duration_s"] < cfg.min_duration_s):
            i = j + 1
            continue
        conf = min(1.0, 0.6
                   + 0.20 * min(1.0, net_climb / 2000.0)
                   + 0.15 * min(1.0, abs(m["signed_turn_total_deg"]) / 3600.0))
        out.append(Detection(
            type="thermalling",
            start_idx=i, end_idx=j,
            start_ts=samples[i].point.ts_unix, end_ts=samples[j].point.ts_unix,
            confidence=conf,
            explanation=f"continuous {sign:+d}-sense turn, gained {net_climb:.0f} ft in {m['duration_s']:.0f}s",
            evidence={**m, "net_climb_ft": net_climb},
        ))
        i = j + 1
    return _merge_adjacent(out)


# ---------------------------------------------------------------------------
# 12. Sightseeing orbit (helicopter / scenic flight)
# ---------------------------------------------------------------------------
# Slow, wide circling at constant altitude, often well away from any airport.
# Differentiates from TAP by being bigger (radius > 0.3 nm) and not centred on
# a pinpoint (relaxed centre-spread).

@dataclass
class SightseeingConfig:
    min_signed_turn_deg: float = 360.0
    min_radius_nm: float = 0.3
    max_radius_nm: float = 3.0
    max_alt_range_ft: float = 500.0
    min_duration_s: float = 90.0
    min_dist_from_airport_nm: float = 3.0


def detect_sightseeing_orbit(samples: Sequence[Sample], cfg: SightseeingConfig = SightseeingConfig()) -> list[Detection]:
    n = len(samples)
    out: list[Detection] = []
    i = 0
    while i < n - 1:
        if abs(samples[i].turn_rate_dps) < 0.5:
            i += 1
            continue
        sign = 1 if samples[i].turn_rate_dps > 0 else -1
        j = i
        while j + 1 < n:
            nxt = samples[j + 1]
            if (nxt.turn_rate_dps * sign) < 0 and abs(nxt.turn_rate_dps) > 0.5:
                break
            j += 1
        if j - i < 15:
            i += 1
            continue
        m = _slice_metrics(samples, i, j)
        if (abs(m["signed_turn_total_deg"]) < cfg.min_signed_turn_deg
                or m["duration_s"] < cfg.min_duration_s
                or m["alt_range"] > cfg.max_alt_range_ft):
            i = j + 1
            continue
        mid = samples[(i + j) // 2].point
        ap, dist = A.nearest_airport(mid.lat, mid.lon)
        if ap is not None and dist < cfg.min_dist_from_airport_nm:
            i = j + 1
            continue
        radius = G.orbit_radius_nm(m["turn_rate_p90"], max(1.0, m["gs_mean"]))
        if not math.isfinite(radius) or radius < cfg.min_radius_nm or radius > cfg.max_radius_nm:
            i = j + 1
            continue
        conf = min(1.0, 0.55
                   + 0.15 * min(1.0, m["duration_s"] / 360.0)
                   + 0.15 * (1.0 - min(1.0, m["alt_range"] / cfg.max_alt_range_ft))
                   + 0.10 * (1.0 if dist > cfg.min_dist_from_airport_nm else 0.0))
        out.append(Detection(
            type="sightseeing_orbit",
            start_idx=i, end_idx=j,
            start_ts=samples[i].point.ts_unix, end_ts=samples[j].point.ts_unix,
            confidence=conf,
            explanation=f"~{radius:.1f} nm radius orbit, {dist:.1f} nm from {ap.icao if ap else 'airport'}, alt range {m['alt_range']:.0f} ft",
            evidence={**m, "radius_nm": radius, "nearest_airport": ap.icao if ap else None,
                      "dist_nm": dist},
        ))
        i = j + 1
    return _merge_adjacent(out, gap_s=60.0)


# ---------------------------------------------------------------------------
# Public registry — run them all
# ---------------------------------------------------------------------------

# A detector is a callable: samples, *, type_code? -> list[Detection]
Detector = Callable[..., list[Detection]]

DETECTORS: dict[str, Detector] = {
    "steep_turn": lambda s, t="": detect_steep_turn(s),
    "s_turns_across_road": lambda s, t="": detect_s_turns(s),
    "turn_around_a_point": lambda s, t="": detect_turn_around_point(s),
    "chandelle": lambda s, t="": detect_chandelle(s),
    "lazy_8": lambda s, t="": detect_lazy_8(s),
    "slow_flight": lambda s, t="": detect_slow_flight(s, t),
    "stall_recovery": lambda s, t="": detect_stall_recovery(s, t),
    "emergency_descent": lambda s, t="": detect_emergency_descent(s),
    "holding_pattern": lambda s, t="": detect_holding_pattern(s),
    # NB: this one detector emits Detections of TWO types — "touch_and_go" or
    # "landed_full_stop" — depending on the outcome classifier. The registry
    # key here just names the detector function.
    "touch_and_go_or_full_stop": lambda s, t="": detect_touch_and_go(s),
    "thermalling": lambda s, t="": detect_thermalling(s, t),
    "sightseeing_orbit": lambda s, t="": detect_sightseeing_orbit(s),
}


def detect_all(samples: Sequence[Sample], type_code: str = "") -> list[Detection]:
    """Run every registered detector and return a single merged + sorted list."""
    out: list[Detection] = []
    for name, fn in DETECTORS.items():
        try:
            out.extend(fn(samples, type_code))
        except Exception:
            # Detectors must not crash the whole pipeline.
            continue
    out.sort(key=lambda d: (d.start_ts, d.type))
    return out
