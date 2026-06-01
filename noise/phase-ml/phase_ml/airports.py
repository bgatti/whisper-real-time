"""Airport / runway / standard-entry-corridor database.

Each airport has:
    - centre lat/lon and field elevation
    - one or more Runway records with threshold coordinates and heading
    - traffic-pattern altitude (TPA, in MSL ft) and pattern direction
    - standard 'entry corridors' — geometric regions that typical inbound traffic
      passes through (extended runway centerline, 45° downwind entry, etc.).

The runway threshold coordinates were derived from the published runway heading +
the airport reference point by shifting half the runway length along the runway
axis. They are accurate to ~50 ft, plenty for "are you on this runway" geometry.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .geometry import (
    DEG_TO_RAD,
    RunwayFrame,
    haversine_nm,
    lat_lon_to_xy_nm,
    xy_nm_to_lat_lon,
)


@dataclass(frozen=True)
class Runway:
    name: str             # "08", "26L", etc.
    heading_deg: float    # true (we treat published magnetic as true; close enough at <8° variation)
    threshold_lat: float
    threshold_lon: float
    length_ft: float
    pattern: str          # "left" or "right"

    def frame(self, field_elev_ft: float) -> RunwayFrame:
        return RunwayFrame(
            threshold_lat=self.threshold_lat,
            threshold_lon=self.threshold_lon,
            heading_deg=self.heading_deg,
            field_elev_ft=field_elev_ft,
        )


@dataclass(frozen=True)
class Airport:
    icao: str
    name: str
    lat: float
    lon: float
    field_elev_ft: float
    tpa_msl_ft: float       # traffic-pattern altitude
    runways: tuple[Runway, ...] = field(default_factory=tuple)

    def tpa_agl_ft(self) -> float:
        return self.tpa_msl_ft - self.field_elev_ft

    def distance_nm(self, lat: float, lon: float) -> float:
        return haversine_nm(lat, lon, self.lat, self.lon)


# ---------- Built-in airport database (Front Range Colorado) ----------
# Pattern altitudes: standard 1000 AGL for piston, 1500 AGL for jets at KBJC/KAPA.
# Where the airport publishes a TPA in the AFD that overrides standard, we use it.

def _runway_pair(
    airport_lat: float,
    airport_lon: float,
    heading: float,
    length_ft: float,
    primary_name: str,
    primary_pattern: str,
    reciprocal_name: str,
    reciprocal_pattern: str,
) -> tuple[Runway, Runway]:
    """Build two Runway records (primary + reciprocal) from the airport centre.

    The threshold for the primary runway sits half-length back from the airport
    reference point, opposite the departure heading; the reciprocal threshold sits
    the same distance the other way.
    """
    import math

    half_nm = (length_ft / 2.0) / 6076.12
    theta = heading * DEG_TO_RAD
    # The primary runway's threshold is *opposite* its departure heading, so we
    # walk backwards along the runway by half_nm.
    x_back = -math.sin(theta) * half_nm
    y_back = -math.cos(theta) * half_nm
    plat, plon = xy_nm_to_lat_lon(x_back, y_back, airport_lat, airport_lon)
    # Reciprocal: the threshold of the recip is at the *other* end (where the
    # primary runway points), so walk forward.
    rlat, rlon = xy_nm_to_lat_lon(-x_back, -y_back, airport_lat, airport_lon)
    primary = Runway(
        name=primary_name, heading_deg=heading,
        threshold_lat=plat, threshold_lon=plon,
        length_ft=length_ft, pattern=primary_pattern,
    )
    reciprocal = Runway(
        name=reciprocal_name, heading_deg=(heading + 180.0) % 360.0,
        threshold_lat=rlat, threshold_lon=rlon,
        length_ft=length_ft, pattern=reciprocal_pattern,
    )
    return primary, reciprocal


def _build_airports() -> dict[str, Airport]:
    out: dict[str, Airport] = {}

    # KBDU — Boulder Municipal
    p, r = _runway_pair(40.0394, -105.2258, 80.0, 4100, "08", "left", "26", "left")
    out["KBDU"] = Airport(
        icao="KBDU", name="Boulder Municipal",
        lat=40.0394, lon=-105.2258, field_elev_ft=5288, tpa_msl_ft=6300,
        runways=(p, r),
    )
    # KBJC — Rocky Mountain Metropolitan (parallel 12L/30R and 12R/30L). One pair suffices for intent.
    p, r = _runway_pair(39.9088, -105.1172, 119.0, 9000, "12", "right", "30", "left")
    out["KBJC"] = Airport(
        icao="KBJC", name="Rocky Mountain Metropolitan",
        lat=39.9088, lon=-105.1172, field_elev_ft=5673, tpa_msl_ft=7173,  # 1500 AGL
        runways=(p, r),
    )
    # KEIK — Erie Municipal
    p, r = _runway_pair(40.0103, -105.0489, 152.0, 4700, "15", "left", "33", "left")
    out["KEIK"] = Airport(
        icao="KEIK", name="Erie Municipal",
        lat=40.0103, lon=-105.0489, field_elev_ft=5130, tpa_msl_ft=6130,
        runways=(p, r),
    )
    # KLMO — Vance Brand (Longmont)
    p, r = _runway_pair(40.1639, -105.1633, 113.0, 4800, "11", "left", "29", "left")
    out["KLMO"] = Airport(
        icao="KLMO", name="Vance Brand (Longmont)",
        lat=40.1639, lon=-105.1633, field_elev_ft=5054, tpa_msl_ft=6054,
        runways=(p, r),
    )
    # KAPA — Centennial (3 parallel runways simplified to one pair)
    p, r = _runway_pair(39.5701, -104.8492, 174.0, 10001, "17", "left", "35", "right")
    out["KAPA"] = Airport(
        icao="KAPA", name="Centennial",
        lat=39.5701, lon=-104.8492, field_elev_ft=5885, tpa_msl_ft=7385,  # 1500 AGL
        runways=(p, r),
    )
    # KGXY — Greeley-Weld County
    p, r = _runway_pair(40.4375, -104.6333, 90.0, 10000, "09", "left", "27", "left")
    out["KGXY"] = Airport(
        icao="KGXY", name="Greeley-Weld County",
        lat=40.4375, lon=-104.6333, field_elev_ft=4658, tpa_msl_ft=5658,
        runways=(p, r),
    )
    # KFNL — Northern Colorado Regional (Fort Collins-Loveland)
    p, r = _runway_pair(40.4519, -105.0114, 60.0, 8500, "06", "left", "24", "left")
    out["KFNL"] = Airport(
        icao="KFNL", name="Northern Colorado Regional",
        lat=40.4519, lon=-105.0114, field_elev_ft=5016, tpa_msl_ft=6016,
        runways=(p, r),
    )
    # KDEN — Denver International (one of six runways suffices for inbound intent)
    p, r = _runway_pair(39.8617, -104.6731, 80.0, 12000, "08", "left", "26", "left")
    out["KDEN"] = Airport(
        icao="KDEN", name="Denver International",
        lat=39.8617, lon=-104.6731, field_elev_ft=5431, tpa_msl_ft=6931,  # 1500 AGL
        runways=(p, r),
    )
    return out


AIRPORTS: dict[str, Airport] = _build_airports()


def all_airports() -> Iterable[Airport]:
    return AIRPORTS.values()


def get(icao: str) -> Airport:
    return AIRPORTS[icao]


def nearest_airport(lat: float, lon: float, *, max_nm: float = 100.0) -> tuple[Airport | None, float]:
    """Return (airport, distance_nm) or (None, inf) if nothing within max_nm."""
    best: Airport | None = None
    best_d = float("inf")
    for ap in AIRPORTS.values():
        d = ap.distance_nm(lat, lon)
        if d < best_d:
            best_d = d
            best = ap
    if best_d > max_nm:
        return None, float("inf")
    return best, best_d


def candidate_airports(lat: float, lon: float, *, max_nm: float = 30.0) -> list[tuple[Airport, float]]:
    """All airports within max_nm, sorted nearest-first."""
    out: list[tuple[Airport, float]] = []
    for ap in AIRPORTS.values():
        d = ap.distance_nm(lat, lon)
        if d <= max_nm:
            out.append((ap, d))
    out.sort(key=lambda t: t[1])
    return out
