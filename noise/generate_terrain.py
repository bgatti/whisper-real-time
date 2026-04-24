"""Generate terrain.json for the client-side noise model.

Queries SRTM at ~90 m resolution over a box centered on KBDU and writes a
compact JSON file that the browser can load for per-cell / per-point AGL
calculations.

Usage:
    python -m noise.generate_terrain          # writes to noise/web/public/terrain.json
    python -m noise.generate_terrain --half-km 20 --cell-m 90
"""

from __future__ import annotations

import argparse
import json
import math
import time

import numpy as np
import srtm

KBDU_LAT = 40.0394
KBDU_LON = -105.2258
FT_PER_M = 3.28084


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--half-km", type=float, default=18,
                    help="half-size of terrain grid in km (default 18)")
    ap.add_argument("--cell-m", type=float, default=90,
                    help="cell size in meters (default 90, matches SRTM)")
    ap.add_argument("--out", default="noise/web/public/terrain.json")
    args = ap.parse_args()

    lat0, lon0 = KBDU_LAT, KBDU_LON
    half_km = args.half_km
    cell_m = args.cell_m
    n = int(round(2 * half_km * 1000 / cell_m))

    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(lat0))

    print(f"Generating {n}x{n} terrain grid ({n*n} cells), "
          f"half_km={half_km}, cell_m={cell_m}")

    data = srtm.get_data()
    axis = (np.arange(n) - (n - 1) / 2) * cell_m
    xs, ys = np.meshgrid(axis, axis)  # east, north in meters

    t0 = time.perf_counter()
    elev = []
    for r in range(n):
        for c in range(n):
            lat = lat0 + ys[r, c] / m_per_deg_lat
            lon = lon0 + xs[r, c] / m_per_deg_lon
            e = data.get_elevation(lat, lon)
            if e is not None:
                elev.append(round(e * FT_PER_M))
            else:
                elev.append(5300)  # fallback
    dt = time.perf_counter() - t0
    print(f"SRTM lookups: {dt:.1f}s")

    payload = {
        "lat0": lat0,
        "lon0": lon0,
        "half_km": half_km,
        "cell_m": cell_m,
        "n": n,
        "elev_ft": elev,
    }

    with open(args.out, "w") as f:
        json.dump(payload, f, separators=(",", ":"))

    size_kb = len(json.dumps(payload, separators=(",", ":"))) / 1024
    print(f"Wrote {args.out} ({size_kb:.0f} KB, {n}x{n} grid)")
    print(f"Elevation range: {min(elev)}–{max(elev)} ft")


if __name__ == "__main__":
    main()
