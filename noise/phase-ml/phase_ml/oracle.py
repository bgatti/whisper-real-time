"""Rule-based phase classifier — the 'oracle' used for weak labelling.

Port of the JavaScript `classifyAircraft` (see `noise/web/PHASE_ML_KICKOFF.md` for
the original). This implementation generalises beyond a single home field: given
*any* airport from `airports.py`, it returns one of the 9 base labels:

    on_ground, taxiing, pattern, landed_full_stop, practice_area,
    departing, inbound, en_route, nearby

`landed_full_stop` requires a multi-window view (the rule fires only after
sustained on-ground time + dwell), so it's computed in `classify_track(...)`
rather than per-window.

This is intentionally simple and noisy. The point of the oracle is to give us
~95% useful weak labels cheaply; the maneuver detector and ML model handle the
hard cases the oracle gets wrong.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from . import airports as A
from . import geometry as G
from .data_loader import Track
from .features import Sample, enrich

# Default thresholds. Override per-field if needed via `OracleConfig`.
DEFAULT_ON_GROUND_AGL_FT = 200.0
DEFAULT_TAXI_AGL_FT = 150.0
DEFAULT_PATTERN_DIST_NM = 2.5
DEFAULT_PATTERN_MIN_AGL = 100.0
DEFAULT_PATTERN_MAX_AGL = 1500.0
DEFAULT_PRACTICE_DIST_NM = (2.0, 8.0)
DEFAULT_PRACTICE_AGL = (200.0, 3000.0)
DEFAULT_DEPART_TRACK_OFFSET_DEG = 60.0
DEFAULT_INBOUND_TRACK_OFFSET_DEG = 40.0
DEFAULT_INBOUND_RANGE_NM = (1.5, 50.0)
DEFAULT_EN_ROUTE_DIST_NM = 8.0

LANDED_REQUIRED_GROUND_S = 30.0
LANDED_REQUIRED_DWELL_S = 300.0
LANDED_NO_NEW_TAKEOFF_S = 900.0


@dataclass
class OracleConfig:
    on_ground_agl_ft: float = DEFAULT_ON_GROUND_AGL_FT
    taxi_agl_ft: float = DEFAULT_TAXI_AGL_FT
    pattern_dist_nm: float = DEFAULT_PATTERN_DIST_NM
    pattern_min_agl: float = DEFAULT_PATTERN_MIN_AGL
    pattern_max_agl: float = DEFAULT_PATTERN_MAX_AGL
    practice_dist_nm: tuple[float, float] = DEFAULT_PRACTICE_DIST_NM
    practice_agl: tuple[float, float] = DEFAULT_PRACTICE_AGL
    depart_track_offset_deg: float = DEFAULT_DEPART_TRACK_OFFSET_DEG
    inbound_track_offset_deg: float = DEFAULT_INBOUND_TRACK_OFFSET_DEG
    inbound_range_nm: tuple[float, float] = DEFAULT_INBOUND_RANGE_NM
    en_route_dist_nm: float = DEFAULT_EN_ROUTE_DIST_NM


@dataclass
class OracleLabel:
    phase: str           # one of the 9 labels
    airport: str | None  # ICAO this label is measured against
    dist_nm: float
    agl_ft: float
    track_off_deg: float  # |Δ| between current track and the bearing-to-field

    def __str__(self) -> str:
        return f"{self.phase:<18s} | {self.airport or '—':<5s} | d={self.dist_nm:5.2f} nm  AGL={self.agl_ft:5.0f} ft"


def classify_sample(
    sample: Sample,
    airport: A.Airport,
    cfg: OracleConfig = OracleConfig(),
) -> OracleLabel:
    """Per-sample classification (no temporal context).

    `landed_full_stop` is NEVER returned here — use `classify_track` for that.
    """
    p = sample.point
    dist = airport.distance_nm(p.lat, p.lon)
    agl = p.alt_msl_ft - airport.field_elev_ft
    gs = sample.gs_kts
    vs = sample.vs_fpm
    bearing_to_field = G.bearing_deg(p.lat, p.lon, airport.lat, airport.lon)
    track_off = G.angle_diff_abs(sample.track_deg, bearing_to_field)

    out = lambda phase: OracleLabel(phase, airport.icao, dist, agl, track_off)

    if gs < 30 and agl < cfg.on_ground_agl_ft and dist < 2.0:
        return out("on_ground")
    if 5 <= gs < 40 and agl < cfg.taxi_agl_ft and dist < 1.5:
        return out("taxiing")
    if dist < cfg.pattern_dist_nm and cfg.pattern_min_agl < agl < cfg.pattern_max_agl and 40 < gs < 130:
        return out("pattern")
    if (cfg.practice_dist_nm[0] < dist < cfg.practice_dist_nm[1]
            and cfg.practice_agl[0] < agl < cfg.practice_agl[1]
            and track_off > 40 and vs > -500):
        return out("practice_area")
    if agl > 200 and vs > 200 and track_off > cfg.depart_track_offset_deg and dist < 15:
        return out("departing")
    if (track_off < cfg.inbound_track_offset_deg
            and cfg.inbound_range_nm[0] < dist < cfg.inbound_range_nm[1]
            and gs > 30 and vs <= 300):
        return out("inbound")
    if dist > cfg.en_route_dist_nm:
        return out("en_route")
    return out("nearby")


def classify_track(
    track: Track,
    airport: A.Airport | None = None,
    cfg: OracleConfig = OracleConfig(),
) -> list[OracleLabel]:
    """Classify every sample in a track and overlay `landed_full_stop`.

    If `airport` is None, the nearest airport at each sample is used.
    """
    samples = enrich(track.points)
    labels = _per_sample_labels(samples, airport, cfg)
    _overlay_landed_full_stop(samples, labels, cfg)
    return labels


def _per_sample_labels(
    samples: Sequence[Sample],
    airport: A.Airport | None,
    cfg: OracleConfig,
) -> list[OracleLabel]:
    out: list[OracleLabel] = []
    for s in samples:
        if airport is not None:
            ap = airport
        else:
            ap, _ = A.nearest_airport(s.point.lat, s.point.lon)
            if ap is None:
                # No airport in range; emit a degenerate label
                out.append(OracleLabel("nearby", None, float("inf"), s.point.alt_msl_ft, 0.0))
                continue
        out.append(classify_sample(s, ap, cfg))
    return out


def _overlay_landed_full_stop(
    samples: Sequence[Sample],
    labels: list[OracleLabel],
    cfg: OracleConfig,
) -> None:
    """Mutate labels in place: convert qualifying on_ground runs to landed_full_stop.

    Rules (post hoc, with hindsight):
      - The run must be ≥ LANDED_REQUIRED_GROUND_S of `on_ground` or `taxiing`.
      - The run (or its taxi/dwell continuation) must persist for ≥ LANDED_REQUIRED_DWELL_S.
      - No new takeoff (any non-ground label) within LANDED_NO_NEW_TAKEOFF_S of the run's start.
    """
    n = len(samples)
    i = 0
    while i < n:
        if labels[i].phase not in ("on_ground", "taxiing"):
            i += 1
            continue
        # Find the end of this ground/taxi run
        j = i
        while j < n and labels[j].phase in ("on_ground", "taxiing"):
            j += 1
        # j is one past the last ground index
        run_start = samples[i].point.ts_unix
        run_end = samples[j - 1].point.ts_unix
        run_duration = run_end - run_start
        if run_duration < LANDED_REQUIRED_GROUND_S:
            i = j
            continue
        # Forward-look for a new takeoff within NO_NEW_TAKEOFF_S
        deadline = run_start + LANDED_NO_NEW_TAKEOFF_S
        new_takeoff = False
        for k in range(j, n):
            if samples[k].point.ts_unix > deadline:
                break
            if labels[k].phase not in ("on_ground", "taxiing"):
                new_takeoff = True
                break
        if new_takeoff:
            i = j
            continue
        # Total dwell = run_duration + any further on-ground time within deadline.
        # If the track simply ends, run_duration alone counts; require >= REQUIRED_DWELL_S
        # only when continuation data is available.
        eligible = (run_duration >= LANDED_REQUIRED_DWELL_S) or (j == n)
        if not eligible:
            i = j
            continue
        # Promote every sample in the run to landed_full_stop.
        for k in range(i, j):
            labels[k] = OracleLabel(
                phase="landed_full_stop",
                airport=labels[k].airport,
                dist_nm=labels[k].dist_nm,
                agl_ft=labels[k].agl_ft,
                track_off_deg=labels[k].track_off_deg,
            )
        i = j
