"""Pure geometry primitives.

All angles are in degrees on the wire (lat/lon, headings, bearings) and converted
to radians internally only where math.sin/cos need them. Distances default to
nautical miles; altitudes to feet.

The 'flat-Earth' helpers are exact enough at <50 nm scales (Boulder corridor is
~40 nm wide) and far cheaper than haversine in hot loops. Use haversine_nm only
at the boundary (e.g. distance-to-airport once per aircraft per tick).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

# WGS-84 enough; aviation uses spherical-earth for ATC math anyway.
EARTH_RADIUS_NM = 3440.065
FT_PER_NM = 6076.12
DEG_TO_RAD = math.pi / 180.0
RAD_TO_DEG = 180.0 / math.pi
KT_TO_FPS = 1.68781  # nautical knots to feet per second
G_FT_S2 = 32.174     # gravitational acceleration


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in nautical miles."""
    phi1, phi2 = lat1 * DEG_TO_RAD, lat2 * DEG_TO_RAD
    dphi = (lat2 - lat1) * DEG_TO_RAD
    dlam = (lon2 - lon1) * DEG_TO_RAD
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * EARTH_RADIUS_NM * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """True bearing from point 1 to point 2, in degrees [0, 360)."""
    phi1, phi2 = lat1 * DEG_TO_RAD, lat2 * DEG_TO_RAD
    dlam = (lon2 - lon1) * DEG_TO_RAD
    y = math.sin(dlam) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlam)
    return (math.atan2(y, x) * RAD_TO_DEG + 360.0) % 360.0


def angle_diff_deg(a: float, b: float) -> float:
    """Signed smallest angular difference a - b, wrapped to (-180, 180]."""
    d = ((a - b + 180.0) % 360.0) - 180.0
    # Map -180 to +180 so the interval is (-180, 180].
    return 180.0 if d == -180.0 else d


def angle_diff_abs(a: float, b: float) -> float:
    """Absolute smallest angular difference in [0, 180]."""
    return abs(angle_diff_deg(a, b))


def normalize_bearing(b: float) -> float:
    """Wrap to [0, 360)."""
    return b % 360.0


# ---------- Local-tangent (flat-Earth) frame ----------
# A linear projection centred on (lat0, lon0). Errors < 0.1% within ~30 nm.

def lat_lon_to_xy_nm(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    """Local east/north offset in nautical miles from (lat0, lon0)."""
    dlat = lat - lat0
    dlon = lon - lon0
    cos_lat0 = math.cos(lat0 * DEG_TO_RAD)
    x_nm = dlon * 60.0 * cos_lat0   # 60 nm per degree of longitude * cos(lat)
    y_nm = dlat * 60.0              # 60 nm per degree of latitude
    return x_nm, y_nm


def xy_nm_to_lat_lon(x_nm: float, y_nm: float, lat0: float, lon0: float) -> tuple[float, float]:
    cos_lat0 = math.cos(lat0 * DEG_TO_RAD)
    return lat0 + y_nm / 60.0, lon0 + x_nm / (60.0 * cos_lat0)


# ---------- Runway frame ----------
# Along-track / cross-track distance from a runway threshold, taking the runway
# heading into account. Lets us answer "how far short of the threshold is this
# aircraft, and how far off the centerline?" in nm.

@dataclass(frozen=True)
class RunwayFrame:
    """A coordinate frame aligned with a runway threshold + heading."""
    threshold_lat: float
    threshold_lon: float
    heading_deg: float   # runway heading, 0-360
    field_elev_ft: float

    def project(self, lat: float, lon: float) -> tuple[float, float]:
        """Return (along_nm, cross_nm) in the runway frame.

        along_nm is +ve out the departure end of the runway, -ve before threshold
        (i.e. an aircraft on final has along < 0 and decreasing toward 0 on touchdown).
        cross_nm is +ve to the right of the runway centerline (looking down-runway).
        """
        x, y = lat_lon_to_xy_nm(lat, lon, self.threshold_lat, self.threshold_lon)
        # Rotate world frame (east=+x, north=+y) into runway frame.
        # Runway heading is measured clockwise from north; we want along = +heading direction.
        theta = self.heading_deg * DEG_TO_RAD
        sin_t, cos_t = math.sin(theta), math.cos(theta)
        along = x * sin_t + y * cos_t
        cross = x * cos_t - y * sin_t
        return along, cross


# ---------- Path geometry over a polyline ----------

def haversine_path_length_nm(points: Sequence[Sequence[float]]) -> float:
    """Sum of haversine segment lengths. points = [(lat, lon, ...), ...]."""
    total = 0.0
    for i in range(1, len(points)):
        total += haversine_nm(points[i - 1][0], points[i - 1][1], points[i][0], points[i][1])
    return total


def displacement_nm(points: Sequence[Sequence[float]]) -> float:
    if len(points) < 2:
        return 0.0
    return haversine_nm(points[0][0], points[0][1], points[-1][0], points[-1][1])


def signed_heading_change(prev_heading: float, new_heading: float) -> float:
    """Signed heading change, +ve = right turn, -ve = left turn. Range (-180, 180]."""
    return angle_diff_deg(new_heading, prev_heading)


def cumulative_signed_turn_deg(headings: Sequence[float]) -> float:
    """Net turn in degrees. +ve right, -ve left. Unbounded (a full right orbit = +360)."""
    total = 0.0
    for i in range(1, len(headings)):
        total += signed_heading_change(headings[i - 1], headings[i])
    return total


def cumulative_abs_turn_deg(headings: Sequence[float]) -> float:
    """Total absolute angular accumulation. Two 360s in opposite directions = 720."""
    total = 0.0
    for i in range(1, len(headings)):
        total += abs(signed_heading_change(headings[i - 1], headings[i]))
    return total


# ---------- Curvature from a 3-point window ----------

def turn_rate_deg_per_s(prev_heading: float, new_heading: float, dt_s: float) -> float:
    if dt_s <= 0:
        return 0.0
    return signed_heading_change(prev_heading, new_heading) / dt_s


def bank_angle_deg_from_turn_rate(turn_rate_dps: float, groundspeed_kts: float) -> float:
    """Bank angle (deg) implied by a coordinated turn at given rate & TAS (≈GS).

    Coordinated turn:  tan(bank) = (ω * V) / g
    With ω in rad/s and V in ft/s.
    """
    if groundspeed_kts <= 1:
        return 0.0
    omega_rad_s = abs(turn_rate_dps) * DEG_TO_RAD
    v_fps = groundspeed_kts * KT_TO_FPS
    return math.atan2(omega_rad_s * v_fps, G_FT_S2) * RAD_TO_DEG


def orbit_radius_nm(turn_rate_dps: float, groundspeed_kts: float) -> float:
    """Steady-state orbit radius (nm) at given turn rate & groundspeed.

    Inf-norm: a near-straight path returns a very large radius.
    """
    if abs(turn_rate_dps) < 0.05:
        return float("inf")
    omega_rad_s = abs(turn_rate_dps) * DEG_TO_RAD
    v_nm_per_s = groundspeed_kts / 3600.0
    return v_nm_per_s / omega_rad_s


# ---------- Convex-ish helpers ----------

def centroid_latlon(points: Iterable[Sequence[float]]) -> tuple[float, float]:
    """Arithmetic mean of lat/lon. Adequate for small clusters; do not use globally."""
    n = lat_sum = lon_sum = 0
    for p in points:
        lat_sum += p[0]
        lon_sum += p[1]
        n += 1
    if n == 0:
        return float("nan"), float("nan")
    return lat_sum / n, lon_sum / n


def std(values: Sequence[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))
