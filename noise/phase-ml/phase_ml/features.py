"""Windowed feature extraction from a Track.

Every detector in this package consumes a `FeatureWindow`: a coherent slice of
trajectory plus the derived kinematics (groundspeed, vertical speed, track) and
the rolled-up geometric stats (curvature, sinuosity, accumulated heading) over
that slice.

Derivatives are computed locally rather than trusting any field the upstream
ADS-B feed might have provided — the raw archive only carries lat/lon/alt/ts,
and computing gs/vs/track from those four lets us treat archived and live data
identically.

Conventions:
    - One window has at least 2 points (else most features are undefined).
    - Windows are aligned to the **end** of a trailing N-second slice.
    - All geometric stats use the great-circle distance (haversine_nm), so the
      results are accurate over the full corridor regardless of window length.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from . import geometry as G
from .data_loader import Point, Track


# ---------------------------------------------------------------------------
# Per-sample kinematics
# ---------------------------------------------------------------------------

@dataclass
class Sample:
    """One ADS-B fix enriched with locally-computed derivatives."""
    point: Point
    gs_kts: float = 0.0          # ground speed
    vs_fpm: float = 0.0          # vertical speed
    track_deg: float = 0.0       # ground track
    turn_rate_dps: float = 0.0   # heading change per second
    dt_s: float = 0.0            # interval to previous sample (0 at index 0)


# If two consecutive ADS-B fixes are more than MAX_SAMPLE_GAP_S apart, treat
# them as belonging to different flight sessions and reset the derivative
# state. Without this, the archive's habit of concatenating a tail's whole
# year into one record produces ~50 000 kt derived groundspeeds across the
# overnight gaps.
MAX_SAMPLE_GAP_S = 120.0


def enrich(points: Sequence[Point], smooth_n: int = 3) -> list[Sample]:
    """Compute gs/vs/track/turn_rate locally from raw points.

    `smooth_n` is a centred moving-average window applied to gs and vs only;
    track is left raw because averaging two bearings across the discontinuity
    at 359/0 is a foot-gun. The first/last samples get one-sided derivatives.

    If two adjacent points are more than `MAX_SAMPLE_GAP_S` apart in time we
    treat the second as the start of a new session: its dt/gs/vs/turn_rate are
    zeroed and the previous point's track is propagated forward to avoid spurious
    "instantaneous 180° turn" detections.
    """
    n = len(points)
    if n == 0:
        return []
    raw_gs = [0.0] * n
    raw_vs = [0.0] * n
    raw_tk = [0.0] * n
    dts = [0.0] * n
    is_session_break = [False] * n

    for i in range(1, n):
        prev, cur = points[i - 1], points[i]
        dt = cur.ts_unix - prev.ts_unix
        if dt > MAX_SAMPLE_GAP_S or dt <= 0:
            is_session_break[i] = True
            dts[i] = 0.0
            raw_gs[i] = raw_gs[i - 1]      # carry the previous session's last reading
            raw_vs[i] = 0.0
            raw_tk[i] = raw_tk[i - 1]
            continue
        dts[i] = dt
        d_nm = G.haversine_nm(prev.lat, prev.lon, cur.lat, cur.lon)
        raw_gs[i] = (d_nm / dt) * 3600.0
        raw_vs[i] = ((cur.alt_msl_ft - prev.alt_msl_ft) / dt) * 60.0
        raw_tk[i] = G.bearing_deg(prev.lat, prev.lon, cur.lat, cur.lon)

    # First sample: copy forward.
    raw_gs[0] = raw_gs[1] if n > 1 else 0.0
    raw_vs[0] = raw_vs[1] if n > 1 else 0.0
    raw_tk[0] = raw_tk[1] if n > 1 else 0.0

    gs = _smooth_skipping_breaks(raw_gs, is_session_break, smooth_n)
    vs = _smooth_skipping_breaks(raw_vs, is_session_break, smooth_n)

    samples: list[Sample] = []
    for i in range(n):
        if i == 0 or is_session_break[i]:
            tr = 0.0
        else:
            tr = G.turn_rate_deg_per_s(raw_tk[i - 1], raw_tk[i], dts[i])
        samples.append(Sample(
            point=points[i],
            gs_kts=gs[i],
            vs_fpm=vs[i],
            track_deg=raw_tk[i],
            turn_rate_dps=tr,
            dt_s=dts[i],
        ))
    return samples


def _smooth_skipping_breaks(values: Sequence[float], breaks: Sequence[bool], n: int) -> list[float]:
    """Centred moving average that does not cross session-break boundaries."""
    if n <= 1:
        return list(values)
    half = n // 2
    out: list[float] = []
    for i in range(len(values)):
        lo = max(0, i - half)
        hi = min(len(values), i + half + 1)
        # Trim the window so it doesn't span a break.
        for j in range(i, lo - 1, -1):
            if j < i and breaks[j + 1]:
                lo = j + 1
                break
        for j in range(i, hi):
            if j > i and breaks[j]:
                hi = j
                break
        window = values[lo:hi]
        out.append(sum(window) / len(window) if window else values[i])
    return out


def _moving_average(values: Sequence[float], n: int) -> list[float]:
    """Centred moving average; clipped at boundaries. n=1 returns a copy."""
    if n <= 1:
        return list(values)
    half = n // 2
    out: list[float] = []
    for i in range(len(values)):
        lo = max(0, i - half)
        hi = min(len(values), i + half + 1)
        window = values[lo:hi]
        out.append(sum(window) / len(window))
    return out


# ---------------------------------------------------------------------------
# Windowed feature aggregate
# ---------------------------------------------------------------------------

@dataclass
class FeatureWindow:
    """Roll-up of a trailing slice of a track.

    All fields are derived from the underlying samples; nothing here is an input.
    A detector reading this struct should never need to walk the points itself.
    """
    samples: list[Sample]

    # Time / extent
    duration_s: float = 0.0
    track_length_nm: float = 0.0
    displacement_nm: float = 0.0
    sinuosity: float = 1.0          # path length / displacement (>=1; 1 = straight)

    # Speed / energy
    gs_mean_kts: float = 0.0
    gs_min_kts: float = 0.0
    gs_max_kts: float = 0.0
    gs_std_kts: float = 0.0
    vs_mean_fpm: float = 0.0
    vs_min_fpm: float = 0.0
    vs_max_fpm: float = 0.0
    vs_std_fpm: float = 0.0
    alt_start_ft: float = 0.0
    alt_end_ft: float = 0.0
    alt_min_ft: float = 0.0
    alt_max_ft: float = 0.0
    alt_std_ft: float = 0.0
    altitude_reversals: int = 0     # count of vs sign flips above noise floor

    # Heading / turning
    track_start_deg: float = 0.0
    track_end_deg: float = 0.0
    abs_turn_total_deg: float = 0.0    # sum of |Δheading| — bend
    signed_turn_total_deg: float = 0.0  # net turn — +ve right, -ve left
    turn_rate_max_dps: float = 0.0     # signed max of turn_rate (largest abs)
    turn_rate_p95_dps: float = 0.0     # 95th percentile of |turn_rate|
    bank_angle_max_deg: float = 0.0    # max implied bank
    direction_reversals: int = 0       # turn_rate sign flips above noise

    # Track sinuosity decomposition
    avg_orbit_radius_nm: float = float("inf")   # turn-radius if curving

    # Convenience back-references
    point_start: Point | None = None
    point_end: Point | None = None

    # Cached series for detectors that need raw arrays
    gs_series: list[float] = field(default_factory=list)
    vs_series: list[float] = field(default_factory=list)
    alt_series: list[float] = field(default_factory=list)
    track_series: list[float] = field(default_factory=list)
    turn_rate_series: list[float] = field(default_factory=list)


def build_window(samples: Sequence[Sample]) -> FeatureWindow:
    """Compute all derived stats over a slice of samples."""
    n = len(samples)
    w = FeatureWindow(samples=list(samples))
    if n == 0:
        return w
    w.point_start = samples[0].point
    w.point_end = samples[-1].point
    w.duration_s = samples[-1].point.ts_unix - samples[0].point.ts_unix

    gs = [s.gs_kts for s in samples]
    vs = [s.vs_fpm for s in samples]
    alt = [s.point.alt_msl_ft for s in samples]
    tk = [s.track_deg for s in samples]
    tr = [s.turn_rate_dps for s in samples]
    w.gs_series, w.vs_series, w.alt_series = gs, vs, alt
    w.track_series, w.turn_rate_series = tk, tr

    # ----- length / sinuosity -----
    w.track_length_nm = G.haversine_path_length_nm([(s.point.lat, s.point.lon) for s in samples])
    if n >= 2:
        w.displacement_nm = G.haversine_nm(
            samples[0].point.lat, samples[0].point.lon,
            samples[-1].point.lat, samples[-1].point.lon,
        )
    w.sinuosity = (w.track_length_nm / w.displacement_nm) if w.displacement_nm > 1e-6 else float("inf")

    # ----- speed / energy -----
    w.gs_mean_kts = sum(gs) / n
    w.gs_min_kts = min(gs)
    w.gs_max_kts = max(gs)
    w.gs_std_kts = G.std(gs)
    w.vs_mean_fpm = sum(vs) / n
    w.vs_min_fpm = min(vs)
    w.vs_max_fpm = max(vs)
    w.vs_std_fpm = G.std(vs)
    w.alt_start_ft = alt[0]
    w.alt_end_ft = alt[-1]
    w.alt_min_ft = min(alt)
    w.alt_max_ft = max(alt)
    w.alt_std_ft = G.std(alt)
    w.altitude_reversals = _count_sign_reversals(vs, threshold=100.0)

    # ----- heading / turning -----
    w.track_start_deg = tk[0]
    w.track_end_deg = tk[-1]
    w.abs_turn_total_deg = G.cumulative_abs_turn_deg(tk)
    w.signed_turn_total_deg = G.cumulative_signed_turn_deg(tk)

    # turn-rate stats: keep signs for the max but use abs for percentile
    abs_tr = [abs(x) for x in tr]
    if abs_tr:
        i_max = max(range(n), key=lambda i: abs_tr[i])
        w.turn_rate_max_dps = tr[i_max]
        w.turn_rate_p95_dps = _percentile(abs_tr, 0.95)
        w.bank_angle_max_deg = G.bank_angle_deg_from_turn_rate(
            tr[i_max], samples[i_max].gs_kts
        )

    w.direction_reversals = _count_sign_reversals(tr, threshold=1.0)  # 1°/s noise floor

    # Average orbit radius over the window (using mean gs & p95 turn rate).
    w.avg_orbit_radius_nm = G.orbit_radius_nm(w.turn_rate_p95_dps, max(1.0, w.gs_mean_kts))

    return w


def window_track(track: Track, *, window_s: float = 180.0, step_s: float = 30.0) -> list[FeatureWindow]:
    """Slice a Track into overlapping trailing windows.

    Each window covers ~window_s seconds of trajectory; the windows step forward
    by step_s. The result is suitable for offline labelling (one row per window).
    """
    samples = enrich(track.points)
    if len(samples) < 2:
        return []
    out: list[FeatureWindow] = []
    # Build an index of cumulative time
    ts = [s.point.ts_unix for s in samples]
    n = len(samples)
    end = 0
    next_emit = ts[0] + window_s
    while end < n and ts[end] < next_emit:
        end += 1
    while end < n:
        target_end_ts = ts[end]
        target_start_ts = target_end_ts - window_s
        start = end
        while start > 0 and ts[start - 1] >= target_start_ts:
            start -= 1
        out.append(build_window(samples[start:end + 1]))
        next_emit += step_s
        while end < n and ts[end] < next_emit:
            end += 1
    # Always emit the very last window so the latest data is present.
    if not out or out[-1].point_end is not samples[-1].point:
        target_end_ts = ts[-1]
        target_start_ts = target_end_ts - window_s
        start = n - 1
        while start > 0 and ts[start - 1] >= target_start_ts:
            start -= 1
        out.append(build_window(samples[start:n]))
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _count_sign_reversals(values: Sequence[float], threshold: float) -> int:
    """Count zero-crossings, treating |x| < threshold as 'noise / no sign'."""
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


def _percentile(sorted_or_unsorted: Sequence[float], q: float) -> float:
    if not sorted_or_unsorted:
        return 0.0
    s = sorted(sorted_or_unsorted)
    if q <= 0:
        return s[0]
    if q >= 1:
        return s[-1]
    idx = q * (len(s) - 1)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    frac = idx - lo
    return s[lo] + (s[hi] - s[lo]) * frac
