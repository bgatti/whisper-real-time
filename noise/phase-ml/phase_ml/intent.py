"""Multi-airport intent predictor.

Given the recent trajectory of one aircraft, this module estimates the posterior
probability that the aircraft is inbound to each candidate airport (plus a
'no airport / transit' hypothesis). The output is a ranked list of intents
with calibrated scores, an explanation, and the most-likely runway end.

We deliberately do **not** train this from data — it's a hand-crafted score
that combines four lines of evidence, each of which has an obvious aviation
interpretation:

    1. closure_score        — closing distance fast (sign + magnitude)
    2. heading_alignment    — current track points at the airport (within reason)
    3. runway_alignment     — current track matches a runway heading (better!)
    4. energy_match         — altitude/distance fits a normal descent profile
    5. corridor_match       — close to a standard pattern entry corridor
                              (45° to downwind, straight-in 5 nm cone, etc.)

The score for airport i is the product of the per-line scores, and the
posterior is the normalised product across all airports + a transit hypothesis
that gets a flat score.

For full Bayesian rigour you'd want per-aircraft priors (which airport is the
base, where does this tail usually land), and per-airport priors (KAPA gets
more traffic than KLMO). Those go in the optional `prior` argument.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

from . import airports as A
from . import geometry as G
from .features import FeatureWindow, Sample, build_window


# ---------------------------------------------------------------------------
# Score record
# ---------------------------------------------------------------------------

@dataclass
class IntentScore:
    """Hypothesis about which airport an aircraft is heading to."""
    airport: str | None           # ICAO, or None for 'transit'
    runway: str | None            # most-likely runway end (e.g. "26")
    probability: float            # posterior in [0, 1] across all hypotheses
    score_raw: float              # product of evidence scores (un-normalised)
    closure_score: float
    heading_score: float
    runway_score: float
    energy_score: float
    corridor_score: str           # named corridor ("straight_in", "45_downwind", "above_corridor", ...)
    corridor_score_val: float
    distance_nm: float
    bearing_to_field_deg: float
    explanation: str

    def __str__(self) -> str:
        return (f"{self.airport or 'transit':<7s} {self.runway or '--':<4s} "
                f"P={self.probability:.2f} (raw={self.score_raw:.2f})  {self.explanation}")


# ---------------------------------------------------------------------------
# Per-airport evidence
# ---------------------------------------------------------------------------

def _closure_score(window: FeatureWindow, ap: A.Airport) -> tuple[float, float]:
    """How fast is the aircraft closing on this airport (in % of groundspeed)?

    Returns (score, closure_pct). A perfectly inbound aircraft closes at ~100%;
    a tangential aircraft at ~0%; opening at negative.
    """
    if not window.samples or len(window.samples) < 2:
        return 0.0, 0.0
    start = window.samples[0].point
    end = window.samples[-1].point
    d_start = G.haversine_nm(start.lat, start.lon, ap.lat, ap.lon)
    d_end = G.haversine_nm(end.lat, end.lon, ap.lat, ap.lon)
    dt_s = end.ts_unix - start.ts_unix
    if dt_s <= 0:
        return 0.0, 0.0
    closure_nm_min = (d_start - d_end) / dt_s * 60.0   # +ve = closing
    gs_nm_min = max(0.1, window.gs_mean_kts / 60.0)
    closure_pct = closure_nm_min / gs_nm_min
    # Saturating sigmoid: closure_pct > 50% → high score; negative → ~0
    score = 1.0 / (1.0 + math.exp(-6.0 * (closure_pct - 0.30)))
    return score, closure_pct


def _heading_alignment_score(window: FeatureWindow, ap: A.Airport) -> tuple[float, float]:
    """How well does the aircraft's current track point at the airport?

    Returns (score, |Δ|_deg). At very close range (< 1 nm) the bearing-to-field
    is geometrically degenerate (any small position jitter spins the bearing
    wildly), so we taper the influence of this score down to a neutral 0.5 and
    let runway alignment do the work.
    """
    if not window.samples:
        return 0.0, 180.0
    end = window.samples[-1].point
    track = window.track_end_deg
    dist = ap.distance_nm(end.lat, end.lon)
    bearing = G.bearing_deg(end.lat, end.lon, ap.lat, ap.lon)
    delta = G.angle_diff_abs(track, bearing)
    # Geometric score from the bearing match
    geo_score = math.exp(-(delta / 30.0) ** 2)
    # Taper to a neutral 0.5 as distance drops below 2 nm (runway score takes over).
    taper = min(1.0, dist / 2.0)
    score = taper * geo_score + (1.0 - taper) * 0.6
    return score, delta


def _runway_alignment_score(window: FeatureWindow, ap: A.Airport) -> tuple[float, float, str | None]:
    """How well does the track align with any runway heading?

    Returns (score, min_delta_deg, best_runway_name). High score is needed only
    when the aircraft is close enough to be on final / about to enter pattern.
    """
    if not window.samples or not ap.runways:
        return 0.0, 180.0, None
    end = window.samples[-1].point
    dist_nm = ap.distance_nm(end.lat, end.lon)
    # At long range, runway alignment is irrelevant — give a neutral score.
    if dist_nm > 12.0:
        return 0.5, 180.0, None
    track = window.track_end_deg
    best_delta = 180.0
    best_name: str | None = None
    for rw in ap.runways:
        d = G.angle_diff_abs(track, rw.heading_deg)
        if d < best_delta:
            best_delta = d
            best_name = rw.name
    # Tighter tolerance: 20° wide is final, 45° is base, beyond 70° unlikely.
    score = math.exp(-(best_delta / 25.0) ** 2)
    return score, best_delta, best_name


def _energy_match_score(window: FeatureWindow, ap: A.Airport) -> tuple[float, float, float]:
    """Does the altitude/distance match a normal 3° glide-slope arrival?

    A reasonable descent profile is:
        target_agl_at_distance(d) = max(TPA_AGL, d_nm * 318 ft/nm)   # 3° ≈ 318 ft/nm
    Score is high when current AGL is within ~500 ft of the target.
    """
    if not window.samples:
        return 0.0, 0.0, 0.0
    end = window.samples[-1].point
    agl = end.alt_msl_ft - ap.field_elev_ft
    dist_nm = ap.distance_nm(end.lat, end.lon)
    target_agl = max(ap.tpa_agl_ft(), dist_nm * 318.0)
    diff = agl - target_agl
    # Aircraft below profile: still potentially OK (low approach). Above: too high.
    # Sigmoid centered at +500 ft (slightly high is normal), penalising > +1500 ft.
    score = 1.0 / (1.0 + math.exp(0.003 * (diff - 500.0)))
    return score, agl, target_agl


def _corridor_score(window: FeatureWindow, ap: A.Airport) -> tuple[float, str]:
    """How close is the aircraft to a standard pattern-entry corridor?

    We score three named corridors:
        straight_in   — within 1 nm of the extended runway centerline, < 6 nm from threshold
        45_downwind   — 45° entry to downwind for either runway
        above_corridor — above pattern altitude in a wide cone over the field

    Returns the highest-scoring corridor's value + its name.
    """
    if not window.samples or not ap.runways:
        return 0.2, "none"
    end = window.samples[-1].point
    best_score = 0.2
    best_name = "none"
    for rw in ap.runways:
        frame = rw.frame(ap.field_elev_ft)
        along_nm, cross_nm = frame.project(end.lat, end.lon)
        # straight_in: 5 nm short of threshold, ≤ 0.7 nm off centerline, low altitude
        if -8.0 < along_nm < -0.5 and abs(cross_nm) < 0.7:
            score = math.exp(-(abs(cross_nm) / 0.5) ** 2)
            if score > best_score:
                best_score = score
                best_name = f"straight_in_{rw.name}"
        # 45° downwind entry: aircraft sitting on the 45° line off the threshold,
        # on the pattern side, at pattern altitude.
        agl = end.alt_msl_ft - ap.field_elev_ft
        if abs(agl - ap.tpa_agl_ft()) < 400 and 1.0 < abs(cross_nm) < 2.5 and -2.0 < along_nm < 3.0:
            score = math.exp(-((abs(cross_nm) - 1.5) / 1.0) ** 2)
            if score > best_score:
                best_score = score
                best_name = f"45_downwind_{rw.name}"
        # above_corridor: lots of altitude, within 8 nm, regardless of runway
    dist_nm = ap.distance_nm(end.lat, end.lon)
    agl = end.alt_msl_ft - ap.field_elev_ft
    if dist_nm < 8.0 and agl > ap.tpa_agl_ft() + 1500:
        score = 0.6
        if score > best_score:
            best_score = score
            best_name = "above_corridor"
    return best_score, best_name


# ---------------------------------------------------------------------------
# Public predictor
# ---------------------------------------------------------------------------

def predict_intent(
    samples: Sequence[Sample] | FeatureWindow,
    *,
    candidate_airports: Iterable[A.Airport] | None = None,
    max_candidate_nm: float = 50.0,
    prior_by_airport: dict[str, float] | None = None,
    transit_floor: float = 0.10,
) -> list[IntentScore]:
    """Return a posterior ranking over candidate airports + a transit hypothesis.

    samples: a sequence of `Sample` objects (we'll build a FeatureWindow
             internally), or a pre-built FeatureWindow.
    candidate_airports: explicit list; if omitted, all airports within
                        `max_candidate_nm` of the *current* position are considered.
    prior_by_airport: optional prior multipliers (e.g. {'KBDU': 1.5} to encode
                      "this aircraft is based at KBDU").
    transit_floor: minimum score for the 'transit / no airport' hypothesis.
                   Higher = more skeptical of weak airport hypotheses.
    """
    window = samples if isinstance(samples, FeatureWindow) else build_window(samples)
    if not window.samples:
        return []
    end = window.samples[-1].point

    if candidate_airports is None:
        cands = [ap for ap, _ in A.candidate_airports(end.lat, end.lon, max_nm=max_candidate_nm)]
    else:
        cands = list(candidate_airports)

    scores: list[IntentScore] = []
    raw_scores: list[float] = []
    for ap in cands:
        c_score, c_pct = _closure_score(window, ap)
        h_score, h_delta = _heading_alignment_score(window, ap)
        r_score, r_delta, r_name = _runway_alignment_score(window, ap)
        e_score, agl, target_agl = _energy_match_score(window, ap)
        co_score, co_name = _corridor_score(window, ap)
        raw = c_score * h_score * r_score * e_score * co_score
        if prior_by_airport and ap.icao in prior_by_airport:
            raw *= prior_by_airport[ap.icao]

        dist = ap.distance_nm(end.lat, end.lon)
        bearing = G.bearing_deg(end.lat, end.lon, ap.lat, ap.lon)
        explanation = (
            f"dist={dist:.1f} nm closure={c_pct*100:+.0f}% "
            f"Δhead={h_delta:.0f}° "
            f"runway_off={r_delta:.0f}° (best {r_name or 'n/a'}) "
            f"AGL={agl:.0f}/profile={target_agl:.0f} "
            f"corridor={co_name}"
        )
        scores.append(IntentScore(
            airport=ap.icao, runway=r_name,
            probability=0.0, score_raw=raw,
            closure_score=c_score, heading_score=h_score,
            runway_score=r_score, energy_score=e_score,
            corridor_score=co_name, corridor_score_val=co_score,
            distance_nm=dist, bearing_to_field_deg=bearing,
            explanation=explanation,
        ))
        raw_scores.append(raw)

    # Transit hypothesis: a flat floor that doesn't depend on any one airport.
    transit_raw = max(transit_floor, max(raw_scores) * 0.5 if raw_scores else transit_floor)
    transit = IntentScore(
        airport=None, runway=None,
        probability=0.0, score_raw=transit_raw,
        closure_score=0.0, heading_score=0.0, runway_score=0.0,
        energy_score=0.0, corridor_score="transit", corridor_score_val=transit_raw,
        distance_nm=0.0, bearing_to_field_deg=0.0,
        explanation="no airport hypothesis matched well",
    )

    # Normalise.
    total = sum(s.score_raw for s in scores) + transit.score_raw
    if total > 0:
        for s in scores:
            s.probability = s.score_raw / total
        transit.probability = transit.score_raw / total

    ranked = sorted(scores + [transit], key=lambda s: -s.probability)
    return ranked


# ---------------------------------------------------------------------------
# Convenience: full per-track summary
# ---------------------------------------------------------------------------

@dataclass
class IntentSummary:
    top: IntentScore
    runner_up: IntentScore | None
    confidence_gap: float    # top.probability - runner_up.probability
    all_scores: list[IntentScore]


def summarise(samples: Sequence[Sample] | FeatureWindow, **kw) -> IntentSummary:
    scores = predict_intent(samples, **kw)
    if not scores:
        empty = IntentScore(
            airport=None, runway=None, probability=0.0, score_raw=0.0,
            closure_score=0.0, heading_score=0.0, runway_score=0.0,
            energy_score=0.0, corridor_score="none", corridor_score_val=0.0,
            distance_nm=0.0, bearing_to_field_deg=0.0, explanation="no data",
        )
        return IntentSummary(top=empty, runner_up=None, confidence_gap=0.0, all_scores=[])
    top = scores[0]
    ru = scores[1] if len(scores) > 1 else None
    gap = top.probability - (ru.probability if ru else 0.0)
    return IntentSummary(top=top, runner_up=ru, confidence_gap=gap, all_scores=scores)
