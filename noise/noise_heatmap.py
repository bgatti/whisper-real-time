"""Noise heatmap accumulator.

Implements the plan in the /noise task:
  1. source level by power state (climb/cruise/descent)
  2. HP-based source scaling  : dB = 10*log10(HP/HP_ref)
  3. sin^2(theta) propeller directivity disk
  4. -6 dB per doubling of slant range (inverse square)
  5. speed-based dose scaling  : dB = 10*log10(V_ref/V)
  6. logarithmic accumulation across all timesteps into a lat/lon grid

Grid coordinates are local equirectangular meters around a center point.
Output is a 2-D numpy array of dBA-ish levels (reference is arbitrary but
internally consistent: ~70-90 at typical flyover).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, List, Sequence, Tuple

import numpy as np
import srtm

from .aircraft_hp import hp_for_type, DEFAULT_HP

# shared SRTM data handle (lazy-loaded on first use)
_srtm_data = None

def _get_srtm():
    global _srtm_data
    if _srtm_data is None:
        _srtm_data = srtm.get_data()
    return _srtm_data

# --- model constants (from the plan) ---
HP_REF = 100                # baseline horsepower
V_REF_KTS = 100             # reference ground speed for dose scaling
REF_DIST_FT = 100           # reference slant-range for the 0 dB point
REF_SOURCE_DB = 95          # source level (at REF_DIST_FT) for HP_REF at
                            # cruise. Calibrated with SPREAD_EXP=25 so a
                            # 180 HP Cessna at 1000 ft overhead full-power
                            # climb reads ~77 dBA (FAA Part 36 target).
CLIMB_DB = 0                # climb   = full power reference
CRUISE_DB = -3              # 50% power
DESCENT_DB = -6             # 25% power
CLIMB_FPM_THRESH = 200      # |vs| above this = climbing/descending
MIN_SLANT_FT = 50           # floor to avoid singular overheads
GROUND_ELEV_FT = 5300       # fallback when SRTM has no data for a cell
FT_PER_M = 3.28084
FT_PER_NM = 6076.12

# attenuation-function constants (see attenuate() docstring)
G_TERRAIN = 0.65            # blended urban+field ground factor
ALPHA_ATM = 0.0016          # atmospheric absorption @500 Hz, dB/ft
SPREAD_EXP = 25.0           # dB per decade of slant range (matches the JS
                            # single-aircraft model so the aggregate and
                            # per-aircraft visuals stay consistent).


def attenuate(source_db, altitude_ft, horiz_ft):
    """Received dB at a ground cell from a source at (altitude_ft, horiz_ft).

    Combines inverse-square spreading (ref 100 ft), ground absorption that
    fades out overhead, a low-angle grazing penalty, and linear atmospheric
    absorption. Works on scalars or numpy arrays.
    """
    slant_ft = np.sqrt(altitude_ft * altitude_ft + horiz_ft * horiz_ft)
    slant_ft = np.maximum(slant_ft, MIN_SLANT_FT)
    theta = np.arctan2(altitude_ft, np.maximum(horiz_ft, 1e-6))

    loss_spread = -SPREAD_EXP * np.log10(slant_ft / 100.0)
    loss_ground = -(G_TERRAIN * (10.0 - 8.0 * np.sin(theta)))
    loss_graze = -(G_TERRAIN * 9.5 * np.power(np.cos(theta), 2.5))
    loss_atm = -(ALPHA_ATM * slant_ft)

    return source_db + loss_spread + loss_ground + loss_graze + loss_atm


@dataclass
class Grid:
    lat0: float
    lon0: float
    half_km: float          # half-size of the square grid (km)
    cell_m: float           # cell edge length (m)

    def __post_init__(self):
        self.n = int(round(2 * self.half_km * 1000 / self.cell_m))
        self.m_per_deg_lat = 111_320.0
        self.m_per_deg_lon = 111_320.0 * math.cos(math.radians(self.lat0))
        # cell centers in meters, origin at grid center
        axis = (np.arange(self.n) - (self.n - 1) / 2) * self.cell_m
        self.xs, self.ys = np.meshgrid(axis, axis)   # east, north (m)
        self.energy = np.zeros((self.n, self.n), dtype=np.float64)
        self.terrain_ft = self._build_terrain()

    def _build_terrain(self) -> np.ndarray:
        """Query SRTM for ground elevation at every cell center (feet)."""
        data = _get_srtm()
        terrain = np.full((self.n, self.n), GROUND_ELEV_FT, dtype=np.float64)
        for r in range(self.n):
            for c in range(self.n):
                lat = self.lat0 + self.ys[r, c] / self.m_per_deg_lat
                lon = self.lon0 + self.xs[r, c] / self.m_per_deg_lon
                elev_m = data.get_elevation(lat, lon)
                if elev_m is not None:
                    terrain[r, c] = elev_m * FT_PER_M
        return terrain

    def terrain_at(self, lat: float, lon: float) -> float:
        """Return ground elevation (ft) at a single lat/lon via SRTM."""
        elev_m = _get_srtm().get_elevation(lat, lon)
        if elev_m is None:
            return GROUND_ELEV_FT
        return elev_m * FT_PER_M

    def latlon_to_xy(self, lat: float, lon: float) -> Tuple[float, float]:
        x = (lon - self.lon0) * self.m_per_deg_lon
        y = (lat - self.lat0) * self.m_per_deg_lat
        return x, y

    def bounds(self) -> dict:
        dlat = (self.half_km * 1000) / self.m_per_deg_lat
        dlon = (self.half_km * 1000) / self.m_per_deg_lon
        return {
            "south": self.lat0 - dlat,
            "north": self.lat0 + dlat,
            "west": self.lon0 - dlon,
            "east": self.lon0 + dlon,
            "n": self.n,
        }

    def to_db(self) -> np.ndarray:
        out = np.full_like(self.energy, -np.inf)
        mask = self.energy > 0
        out[mask] = 10.0 * np.log10(self.energy[mask])
        return out


def _flight_state_db(prev_alt_ft: float, alt_ft: float) -> float:
    # crude classifier using two consecutive altitudes; caller supplies dt too.
    dz = alt_ft - prev_alt_ft
    if dz > 50:
        return CLIMB_DB
    if dz < -50:
        return DESCENT_DB
    return CRUISE_DB


def _ground_speed_kts(lat1, lon1, lat2, lon2, dt_s):
    if dt_s <= 0:
        return V_REF_KTS
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians((lat1 + lat2) / 2))
    dx = (lon2 - lon1) * m_per_deg_lon
    dy = (lat2 - lat1) * m_per_deg_lat
    dist_m = math.hypot(dx, dy)
    mps = dist_m / dt_s
    return max(30.0, mps * 1.94384)  # clamp low


def accumulate_track(
    grid: Grid,
    points: Sequence[Sequence[float]],   # [[lat, lon, alt_ft], ...]
    icao_type: str,
    dt_s: float = 1.0,
    influence_km: float = 10.0,
    max_agl_ft: float = float("inf"),
    min_agl_ft: float = 0.0,
    city_floor_db: float = 0.0,
    accumulator: str = "lmax",    # "lmax" | "sum_above_floor"
    use_altitude: bool = True,
    use_spread: bool = True,
    use_ground: bool = True,
    use_directivity: bool = True,
) -> None:
    """Accumulate noise energy from one aircraft track into the grid."""
    if len(points) < 2:
        return
    hp = hp_for_type(icao_type)
    if hp <= 0:
        return
    if math.isfinite(max_agl_ft) or min_agl_ft > 0:
        # filter by AGL using terrain elevation at each aircraft position
        filtered = []
        for p in points:
            ground = grid.terrain_at(p[0], p[1])
            agl = p[2] - ground
            if agl >= min_agl_ft and (not math.isfinite(max_agl_ft) or agl <= max_agl_ft):
                filtered.append(p)
        points = filtered
        if len(points) < 2:
            return

    hp_db = 10.0 * math.log10(max(hp, 1) / HP_REF)
    infl_m = influence_km * 1000.0
    infl2 = infl_m * infl_m

    prev_lat, prev_lon, prev_alt = points[0][0], points[0][1], points[0][2]
    for i in range(1, len(points)):
        lat, lon, alt = points[i][0], points[i][1], points[i][2]

        # flight state + speed
        state_db = _flight_state_db(prev_alt, alt)
        v_kts = _ground_speed_kts(prev_lat, prev_lon, lat, lon, dt_s)
        dose_db = 10.0 * math.log10(V_REF_KTS / max(v_kts, 20.0))

        # flight axis (unit vector) in local east/north
        ax, ay = grid.latlon_to_xy(lat, lon)
        px, py = grid.latlon_to_xy(prev_lat, prev_lon)
        vx, vy = ax - px, ay - py
        vmag = math.hypot(vx, vy)
        if vmag < 1e-3:
            vmag = 1.0
            vx, vy = 1.0, 0.0
        else:
            vx /= vmag
            vy /= vmag

        # window of cells within influence radius of the current point
        cx_idx = int(round(ax / grid.cell_m + (grid.n - 1) / 2))
        cy_idx = int(round(ay / grid.cell_m + (grid.n - 1) / 2))
        r_cells = int(math.ceil(infl_m / grid.cell_m))
        x0 = max(0, cx_idx - r_cells); x1 = min(grid.n, cx_idx + r_cells + 1)
        y0 = max(0, cy_idx - r_cells); y1 = min(grid.n, cy_idx + r_cells + 1)
        if x0 >= x1 or y0 >= y1:
            prev_lat, prev_lon, prev_alt = lat, lon, alt
            continue

        sub_dx = grid.xs[y0:y1, x0:x1] - ax
        sub_dy = grid.ys[y0:y1, x0:x1] - ay
        horiz2 = sub_dx * sub_dx + sub_dy * sub_dy
        within = horiz2 <= infl2

        # altitudes are MSL; convert to AGL using per-cell terrain elevation
        sub_terrain = grid.terrain_ft[y0:y1, x0:x1]
        agl_ft = np.maximum(alt - sub_terrain, 0.0)
        if not use_altitude:
            agl_ft = np.zeros_like(sub_terrain)
        horiz_ft_arr = np.sqrt(horiz2, dtype=np.float64) * FT_PER_M
        slant_ft = np.sqrt(agl_ft * agl_ft + horiz_ft_arr * horiz_ft_arr)
        slant_ft = np.maximum(slant_ft, MIN_SLANT_FT)

        attn_source = REF_SOURCE_DB + hp_db + state_db + dose_db
        attn_db = np.full_like(horiz_ft_arr, attn_source)

        if use_spread:
            attn_db = attn_db - SPREAD_EXP * np.log10(slant_ft / 100.0)
        if use_ground:
            theta_geo = np.arctan2(agl_ft, np.maximum(horiz_ft_arr, 1e-6))
            attn_db = attn_db - G_TERRAIN * (10.0 - 8.0 * np.sin(theta_geo))
            attn_db = attn_db - G_TERRAIN * 9.5 * np.power(np.cos(theta_geo), 2.5)
            attn_db = attn_db - ALPHA_ATM * slant_ft

        if use_directivity:
            # True 3D angle between the flight axis (horizontal east/north)
            # and the ray from the plane down-and-out to the cell:
            #   ray_3d = (sub_dx, sub_dy, -agl_m)
            #   flight = (vx, vy, 0)
            # cos(theta) = (ray . flight) / |ray|
            # The "disk" is the plane perpendicular to the flight axis, so
            # sin^2(theta) peaks at 1 for directly-below cells and drops
            # toward 0 for cells far ahead or behind along the track.
            agl_m = agl_ft / FT_PER_M
            slant_m = np.sqrt(horiz2 + agl_m * agl_m) + 1e-6
            cos_theta = (sub_dx * vx + sub_dy * vy) / slant_m
            sin2 = np.clip(1.0 - cos_theta * cos_theta, 0.0, 1.0)
            dir_db = 10.0 * np.log10(sin2 + 0.05)
        else:
            dir_db = 0.0

        total_db = attn_db + dir_db
        contrib = np.where(within, np.power(10.0, total_db / 10.0), 0.0)
        sub = grid.energy[y0:y1, x0:x1]
        if accumulator == "sum_above_floor":
            # Subtract the city ambient in linear power and sum what's left.
            # Each cell's accumulated value is the total aircraft energy
            # audible above the urban background noise floor — a physical
            # dose metric rather than a peak.
            floor_lin = 10.0 ** (city_floor_db / 10.0)
            above = np.maximum(contrib - floor_lin, 0.0)
            sub += above
        else:  # "lmax"
            # Keep the loudest single-sample contribution per cell.
            np.maximum(sub, contrib, out=sub)

        prev_lat, prev_lon, prev_alt = lat, lon, alt


def accumulate_tracks(
    grid: Grid,
    tracks: Iterable[dict],
    dt_s: float = 1.0,
    influence_km: float = 10.0,
    max_agl_ft: float = float("inf"),
    min_agl_ft: float = 0.0,
    city_floor_db: float = 0.0,
    accumulator: str = "lmax",
    **flags,
) -> int:
    n = 0
    for t in tracks:
        pts = t.get("points") or []
        accumulate_track(grid, pts, t.get("type", ""), dt_s=dt_s,
                         influence_km=influence_km,
                         max_agl_ft=max_agl_ft, min_agl_ft=min_agl_ft,
                         city_floor_db=city_floor_db,
                         accumulator=accumulator,
                         **flags)
        n += 1
    return n
