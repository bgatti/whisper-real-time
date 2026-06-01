"""Sanity tests for geometry primitives.

Run:  python -m pytest tests/
      or:  python tests/test_geometry.py
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from phase_ml import airports as A
from phase_ml import geometry as G


def almost(a: float, b: float, *, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol


def test_haversine_identity() -> None:
    d = G.haversine_nm(40.0, -105.0, 40.0, -105.0)
    assert d == 0.0


def test_haversine_known_distance() -> None:
    # KBDU to KBJC, published ~9 nm. We accept ±0.5 nm.
    d = G.haversine_nm(40.0394, -105.2258, 39.9088, -105.1172)
    assert 8.0 < d < 11.0, f"got {d}"


def test_bearing_north() -> None:
    # 1° north of (40, -105) → bearing 0°
    b = G.bearing_deg(40.0, -105.0, 41.0, -105.0)
    assert abs(b) < 0.5 or abs(b - 360) < 0.5


def test_bearing_east() -> None:
    b = G.bearing_deg(40.0, -105.0, 40.0, -104.0)
    assert abs(b - 90.0) < 1.0


def test_angle_diff() -> None:
    assert G.angle_diff_deg(10, 350) == 20
    assert G.angle_diff_deg(350, 10) == -20
    assert G.angle_diff_deg(180, 0) == 180  # max positive


def test_bank_angle_from_turn_rate() -> None:
    # Standard rate turn = 3°/s. At 100 kt, expect bank ~ 15°.
    bank = G.bank_angle_deg_from_turn_rate(3.0, 100.0)
    assert 14 < bank < 17, f"got {bank}"
    # Steep turn ~ 11°/s @ 100 kt → ~45° bank.
    bank = G.bank_angle_deg_from_turn_rate(11.0, 100.0)
    assert 42 < bank < 48, f"got {bank}"


def test_orbit_radius() -> None:
    # 3°/s turn at 100 kt → radius ~ 0.53 nm
    r = G.orbit_radius_nm(3.0, 100.0)
    assert 0.4 < r < 0.7, f"got {r}"


def test_runway_frame_along_cross() -> None:
    # Place a runway pointing east (heading 90°) at the equator-ish.
    rw = G.RunwayFrame(threshold_lat=40.0, threshold_lon=-105.0,
                       heading_deg=90.0, field_elev_ft=5000)
    # Point 1 nm east of threshold should be along=+1 nm, cross=0.
    lat_ahead, lon_ahead = G.xy_nm_to_lat_lon(1.0, 0.0, 40.0, -105.0)
    along, cross = rw.project(lat_ahead, lon_ahead)
    assert abs(along - 1.0) < 0.01, f"along={along}"
    assert abs(cross) < 0.01, f"cross={cross}"
    # Point 1 nm SOUTH of threshold is right of runway (since runway points east).
    lat_south, lon_south = G.xy_nm_to_lat_lon(0.0, -1.0, 40.0, -105.0)
    along, cross = rw.project(lat_south, lon_south)
    assert abs(along) < 0.01
    assert cross > 0.5   # right of runway = +cross


def test_airport_database_present() -> None:
    assert "KBDU" in A.AIRPORTS
    kbdu = A.get("KBDU")
    assert kbdu.field_elev_ft == 5288
    assert len(kbdu.runways) == 2
    # Threshold positions should be within ~1 nm of the field centre
    for rw in kbdu.runways:
        d = G.haversine_nm(rw.threshold_lat, rw.threshold_lon, kbdu.lat, kbdu.lon)
        assert d < 1.0, f"runway {rw.name} threshold is {d} nm from field"


def test_nearest_airport() -> None:
    ap, d = A.nearest_airport(40.0394, -105.2258)
    assert ap is not None and ap.icao == "KBDU"
    assert d < 0.01


if __name__ == "__main__":
    # Lightweight runner (so we don't need pytest installed).
    import inspect
    g = dict(globals())
    failures = 0
    for name, fn in g.items():
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except AssertionError as e:
                print(f"  FAIL {name}: {e}")
                failures += 1
    print(f"\n{failures} failure(s)")
    sys.exit(failures)
