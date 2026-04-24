"""Precompute a matrix of heatmap PNGs by (year, origin).

For every year present in tracks_yearly.json and every origin bucket
(all / local / transient), this script runs the standard accumulation +
colorization pipeline and writes one PNG plus one manifest entry.

The frontend fetches the manifest once and swaps between precomputed PNGs
when the user changes year / origin filters — no on-demand compute, no
jank. School / base filters still fall through to the JS LMax path.

Usage:
  python -m noise.precompute_heatmaps \
    --source noise/web/public/tracks_yearly.json \
    --out-dir noise/web/public/heatmaps \
    --half-km 12 --cell-m 100 --local-radius-nm 5
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
import time
import zlib
from pathlib import Path

import numpy as np

from .noise_heatmap import Grid, accumulate_tracks


LOCAL_RADIUS_NM = 5.0    # "local" = first point within this many nm of center
KBDU = (40.0394, -105.2258)


def _write_png_rgba(path: Path, rgba: np.ndarray) -> None:
    h, w, _ = rgba.shape
    raw = b"".join(b"\x00" + rgba[y].tobytes() for y in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    idat = zlib.compress(raw, 6)
    path.write_bytes(sig + chunk(b"IHDR", ihdr)
                     + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


def _clip_points_to_radius(tracks, lat0, lon0, radius_nm):
    """Keep only the points of each track that lie within radius_nm of
    (lat0, lon0). Tracks with fewer than 2 surviving points are dropped."""
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(lat0))
    r2 = (radius_nm * 1852.0) ** 2
    out = []
    total_in, total_total = 0, 0
    for t in tracks:
        pts = t.get("points") or []
        total_total += len(pts)
        kept = []
        for p in pts:
            dx = (p[1] - lon0) * m_per_deg_lon
            dy = (p[0] - lat0) * m_per_deg_lat
            if dx * dx + dy * dy <= r2:
                kept.append(p)
        total_in += len(kept)
        if len(kept) >= 2:
            out.append({**t, "points": kept})
    return out, total_in, total_total


def _is_local(track, lat0, lon0, local_radius_nm):
    """Origin classification. A track is 'local' if its first reported
    point is within LOCAL_RADIUS_NM of (lat0, lon0) — mirrors the UI rule
    in App.jsx nmFromKBDU(first)."""
    pts = track.get("points") or []
    if not pts:
        return False
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(lat0))
    p0 = pts[0]
    dx = (p0[1] - lon0) * m_per_deg_lon
    dy = (p0[0] - lat0) * m_per_deg_lat
    r2 = (local_radius_nm * 1852.0) ** 2
    return dx * dx + dy * dy <= r2


def _colorize(db, inside_mask, vis_range_db):
    """dB grid → RGBA array (north-up)."""
    masked = np.where(np.isfinite(db) & inside_mask, db, np.nan)
    if np.any(np.isfinite(masked)):
        db_max = float(np.nanmax(masked))
        db_min = float(np.nanmin(masked))
    else:
        return None, None, None

    vis_max = db_max
    span = max(1.0, min(db_max - db_min, vis_range_db))
    vis_min = vis_max - span

    t_raw = (np.where(np.isfinite(db), db, vis_min) - vis_min) / span
    t = np.clip(t_raw, 0.0, 1.0)
    t = np.flipud(t)
    inside_flip = np.flipud(inside_mask)
    finite_flip = np.flipud(np.isfinite(db))

    r = np.where(t < 0.5, 0.0, (t - 0.5) * 2.0)
    g = np.where(t < 0.5, t * 2.0, 1.0 - (t - 0.5) * 2.0)
    b = np.where(t < 0.5, 1.0 - t * 2.0, 0.0)
    alpha = np.where(inside_flip & finite_flip, 40 + 190 * t, 0)

    rgba = np.stack([r * 255, g * 255, b * 255, alpha], axis=-1).astype(np.uint8)
    return rgba, db_min, db_max


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="noise/web/public/tracks_yearly.json")
    ap.add_argument("--out-dir", default="noise/web/public/heatmaps")
    ap.add_argument("--half-km", type=float, default=12.0)
    ap.add_argument("--cell-m", type=float, default=100.0)
    ap.add_argument("--local-radius-nm", type=float, default=LOCAL_RADIUS_NM)
    ap.add_argument("--max-agl-ft", type=float, default=2500.0)
    ap.add_argument("--min-agl-ft", type=float, default=300.0,
                    help="drop track points below this AGL (city-noise floor)")
    ap.add_argument("--influence-km", type=float, default=3.5)
    ap.add_argument("--city-floor-db", type=float, default=55.0,
                    help="ambient urban noise floor; only aircraft energy "
                         "above this contributes to each cell")
    ap.add_argument("--accumulator", default="sum_above_floor",
                    choices=["lmax", "sum_above_floor"])
    ap.add_argument("--vis-range-db", type=float, default=30.0)
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"loading {args.source}...", flush=True)
    data = json.loads(Path(args.source).read_text())
    lat0, lon0 = data.get("center", KBDU)
    tracks = data.get("tracks") or []
    print(f"  {len(tracks)} tracks, center=({lat0}, {lon0})", flush=True)

    # Bucket by year
    by_year = {}
    for t in tracks:
        yr = str(t.get("year") or "unknown")
        by_year.setdefault(yr, []).append(t)
    years = sorted(k for k in by_year.keys() if k != "unknown")
    print(f"  years: {years}", flush=True)

    manifest = {
        "center": [lat0, lon0],
        "half_km": args.half_km,
        "cell_m": args.cell_m,
        "local_radius_nm": args.local_radius_nm,
        "influence_km": args.influence_km,
        "entries": [],
    }

    # Build one PNG per (year, origin) cell, plus one "all years"
    origins = ["all", "local", "transient"]
    slices = []
    for yr in years:
        for orig in origins:
            slices.append((yr, orig, by_year[yr]))
    # plus "all years" × each origin
    for orig in origins:
        slices.append(("all", orig, tracks))

    for year_key, origin_key, base_tracks in slices:
        t0 = time.time()
        # origin filter
        if origin_key == "local":
            filtered = [t for t in base_tracks
                        if _is_local(t, lat0, lon0, args.local_radius_nm)]
        elif origin_key == "transient":
            filtered = [t for t in base_tracks
                        if not _is_local(t, lat0, lon0, args.local_radius_nm)]
        else:
            filtered = base_tracks

        # clip points to 5 nm
        clipped, in_pts, total_pts = _clip_points_to_radius(
            filtered, lat0, lon0, args.local_radius_nm
        )

        grid = Grid(lat0=lat0, lon0=lon0,
                    half_km=args.half_km, cell_m=args.cell_m)
        accumulate_tracks(
            grid, clipped,
            dt_s=1.0,
            influence_km=args.influence_km,
            max_agl_ft=args.max_agl_ft,
            min_agl_ft=args.min_agl_ft,
            city_floor_db=args.city_floor_db,
            accumulator=args.accumulator,
        )
        db = grid.to_db()

        radius_m = args.local_radius_nm * 1852.0
        inside = (grid.xs ** 2 + grid.ys ** 2) <= radius_m ** 2
        rgba, db_min, db_max = _colorize(db, inside, args.vis_range_db)

        slug = f"{year_key}_{origin_key}"
        png_name = f"noise_{slug}.png"
        png_path = out_dir / png_name

        if rgba is None:
            print(f"  {slug:20s}  no data  ({len(filtered)} tracks) - skipped",
                  flush=True)
            continue

        _write_png_rgba(png_path, rgba)
        elapsed = time.time() - t0
        entry = {
            "year": year_key,
            "origin": origin_key,
            "png": png_name,
            "tracks": len(filtered),
            "tracks_with_points": len(clipped),
            "points_in_ring": in_pts,
            "points_total": total_pts,
            "db_min": db_min,
            "db_max": db_max,
            "bounds": grid.bounds(),
        }
        manifest["entries"].append(entry)
        # Write the manifest after every slice so the frontend can pick up
        # partial results during a long run.
        manifest_path = out_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))
        print(f"  {slug:20s}  {len(filtered):5d} tracks -> "
              f"{len(clipped):4d} with pts, peak {db_max:.1f} dB "
              f"({elapsed:.1f}s)", flush=True)

    print(f"\nwrote {len(manifest['entries'])} PNGs + {out_dir}/manifest.json",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
