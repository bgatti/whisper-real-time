#!/usr/bin/env python3
"""Fetch US Census block-group population data for the Boulder / Front Range
area and emit a compact JSON grid suitable for the web population-density
overlay.

Output: noise/web/public/population_density.json

Data sources (no API key needed for these endpoints):
  - Census Bureau ACS 5-year estimates (population by block group)
  - TIGERweb REST services (block group geometries)

Usage:
    python build_population_density.py [--key YOUR_CENSUS_API_KEY]

If no key is supplied the script uses the Census demo key. The demo key is
rate-limited but works for one-off builds.
"""

import argparse
import json
import math
import sys
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.parse import urlencode

# ── Config ───────────────────────────────────────────────────────────────────
STATE_FIPS = "08"  # Colorado

# Counties covering the map viewport (KBDU ± ~30 nm)
COUNTIES = {
    "013": "Boulder",
    "014": "Broomfield",
    "069": "Larimer",
    "123": "Weld",
    "001": "Adams",
    "059": "Jefferson",
    "031": "Denver",
    "005": "Arapahoe",
    "035": "Douglas",
}

# Bounding box for the raster grid (lat/lon)
LAT_MIN, LAT_MAX = 39.50, 40.45
LON_MIN, LON_MAX = -105.55, -104.60

# Grid resolution (pixels)
GRID_W = 400
GRID_H = 400

OUT_PATH = Path(__file__).parent / "web" / "public" / "population_density.json"


def fetch_json(url):
    """GET a URL and parse JSON."""
    req = Request(url, headers={"User-Agent": "noise-density-builder/1.0"})
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


# ── Census API: population by block group ────────────────────────────────────

def fetch_population(api_key):
    """Return {(county, tract, blkgrp): population} for all target counties."""
    pop = {}
    for county_fips in COUNTIES:
        url = (
            f"https://api.census.gov/data/2022/acs/acs5"
            f"?get=B01001_001E"  # total population
            f"&for=block%20group:*"
            f"&in=state:{STATE_FIPS}%20county:{county_fips}"
        )
        if api_key:
            url += f"&key={api_key}"
        try:
            data = fetch_json(url)
        except Exception as e:
            print(f"  WARN: county {county_fips} ({COUNTIES[county_fips]}): {e}")
            continue
        # data[0] is header: [B01001_001E, state, county, tract, block group]
        for row in data[1:]:
            population = int(row[0]) if row[0] else 0
            county = row[2]
            tract = row[3]
            blkgrp = row[4]
            pop[(county, tract, blkgrp)] = population
    print(f"  Fetched population for {len(pop)} block groups")
    return pop


# ── TIGERweb: block group geometries ────────────────────────────────────────

TIGERWEB_BG = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/"
    "TIGERweb/tigerWMS_ACS2022/MapServer/8/query"
)


def fetch_geometries():
    """Return list of {county, tract, blkgrp, rings, area_km2} for all
    block groups in the target counties."""
    geoms = []
    for county_fips in COUNTIES:
        params = {
            "where": f"STATE='{STATE_FIPS}' AND COUNTY='{county_fips}'",
            "outFields": "GEOID,STATE,COUNTY,TRACT,BLKGRP,AREALAND",
            "outSR": "4326",
            "f": "json",
            "returnGeometry": "true",
        }
        url = f"{TIGERWEB_BG}?{urlencode(params)}"
        try:
            data = fetch_json(url)
        except Exception as e:
            print(f"  WARN: geometries for county {county_fips}: {e}")
            continue
        for feat in data.get("features", []):
            attr = feat["attributes"]
            rings = feat["geometry"]["rings"]
            area_m2 = attr.get("AREALAND", 0)
            geoms.append({
                "county": attr["COUNTY"],
                "tract": attr["TRACT"],
                "blkgrp": attr["BLKGRP"],
                "rings": rings,
                "area_km2": area_m2 / 1e6 if area_m2 > 0 else 0,
            })
    print(f"  Fetched geometry for {len(geoms)} block groups")
    return geoms


# ── Rasterize: polygon fill → density grid ──────────────────────────────────

def polygon_area_sq_km(rings):
    """Approximate area in km² using the shoelace formula on lat/lon coords,
    converting degrees to km at the polygon's mean latitude."""
    total = 0.0
    mean_lat = 0.0
    n = 0
    for ring in rings:
        for x, y in ring:
            mean_lat += y
            n += 1
    mean_lat = mean_lat / max(1, n)
    km_per_deg_lat = 111.32
    km_per_deg_lon = 111.32 * math.cos(math.radians(mean_lat))

    for ring in rings:
        area = 0.0
        pts = ring
        for i in range(len(pts)):
            j = (i + 1) % len(pts)
            x1 = pts[i][0] * km_per_deg_lon
            y1 = pts[i][1] * km_per_deg_lat
            x2 = pts[j][0] * km_per_deg_lon
            y2 = pts[j][1] * km_per_deg_lat
            area += x1 * y2 - x2 * y1
        total += area
    return abs(total) / 2.0


def rasterize_block_groups(geoms, pop):
    """Burn block-group population density into a grid. Each cell accumulates
    the density of any block group whose polygon covers that cell center."""
    grid = [[0.0] * GRID_W for _ in range(GRID_H)]
    count_grid = [[0] * GRID_W for _ in range(GRID_H)]

    dx = (LON_MAX - LON_MIN) / GRID_W
    dy = (LAT_MAX - LAT_MIN) / GRID_H

    filled = 0
    for bg in geoms:
        key = (bg["county"], bg["tract"], bg["blkgrp"])
        population = pop.get(key, 0)
        if population <= 0:
            continue
        area_km2 = bg.get("area_km2") or polygon_area_sq_km(bg["rings"])
        if area_km2 < 0.001:
            continue
        density = population / area_km2  # people per km²

        # Bounding box of this polygon in grid coords
        all_lons = [pt[0] for ring in bg["rings"] for pt in ring]
        all_lats = [pt[1] for ring in bg["rings"] for pt in ring]
        min_lon, max_lon = min(all_lons), max(all_lons)
        min_lat, max_lat = min(all_lats), max(all_lats)

        col0 = max(0, int((min_lon - LON_MIN) / dx))
        col1 = min(GRID_W - 1, int((max_lon - LON_MIN) / dx))
        row0 = max(0, int((min_lat - LAT_MIN) / dy))
        row1 = min(GRID_H - 1, int((max_lat - LAT_MIN) / dy))

        for row in range(row0, row1 + 1):
            lat = LAT_MIN + (row + 0.5) * dy
            for col in range(col0, col1 + 1):
                lon = LON_MIN + (col + 0.5) * dx
                if point_in_polygon(lon, lat, bg["rings"]):
                    # Row 0 = LAT_MIN (south), stored bottom-up. We'll flip
                    # for the JSON output (row 0 = north) to match canvas.
                    grid[row][col] = max(grid[row][col], density)
                    count_grid[row][col] += 1
                    filled += 1

    print(f"  Rasterized: {filled} cell fills across {GRID_H}x{GRID_W} grid")
    # Flip so row 0 = north (top of canvas)
    grid.reverse()
    return grid


def point_in_polygon(x, y, rings):
    """Ray-casting point-in-polygon for multi-ring polygons."""
    inside = False
    for ring in rings:
        n = len(ring)
        j = n - 1
        for i in range(n):
            xi, yi = ring[i]
            xj, yj = ring[j]
            if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
                inside = not inside
            j = i
    return inside


# ── Output ───────────────────────────────────────────────────────────────────

def build_output(grid):
    """Write a compact JSON file. Zero-density cells are stored as 0 to keep
    the file small. The web client will ignore them."""
    # Quantize densities to integers (people/km²) — plenty of precision
    quantized = []
    for row in grid:
        quantized.append([int(round(v)) for v in row])

    out = {
        "bounds": {
            "latMin": LAT_MIN,
            "latMax": LAT_MAX,
            "lonMin": LON_MIN,
            "lonMax": LON_MAX,
        },
        "gridW": GRID_W,
        "gridH": GRID_H,
        "unit": "people_per_km2",
        "grid": quantized,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    size_kb = OUT_PATH.stat().st_size / 1024
    print(f"  Wrote {OUT_PATH} ({size_kb:.0f} KB)")


def main():
    parser = argparse.ArgumentParser(description="Build population density grid")
    parser.add_argument("--key", default="", help="Census API key (optional)")
    args = parser.parse_args()

    api_key = args.key or ""

    print("1/4  Fetching block group population from Census API...")
    pop = fetch_population(api_key)
    if not pop:
        print("ERROR: no population data fetched. Supply a Census API key with --key")
        print("       Get one free at: https://api.census.gov/data/key_signup.html")
        sys.exit(1)

    print("2/4  Fetching block group geometries from TIGERweb...")
    geoms = fetch_geometries()
    if not geoms:
        print("ERROR: no geometry data fetched")
        sys.exit(1)

    print("3/4  Rasterizing density grid...")
    grid = rasterize_block_groups(geoms, pop)

    print("4/4  Writing output...")
    build_output(grid)
    print("Done.")


if __name__ == "__main__":
    main()
