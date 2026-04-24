"""CLI: read a tracks.json and emit a low-res noise heatmap PNG + JSON.

The PNG is a raw RGBA color-ramp of the accumulated grid; leaflet's built-in
bilinear upscaling handles smoothing at render time. The sidecar JSON carries
only the geographic bounds and min/max dB so the frontend can overlay it.

Usage:
  python -m noise.build_heatmap \
    --tracks noise/web/public/tracks.json \
    --out    noise/web/public/noise_heatmap.json \
    --half-km 25 --cell-m 300
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import zlib
from pathlib import Path

import numpy as np

from .noise_heatmap import Grid, accumulate_tracks


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tracks", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--half-km", type=float, default=12.0)
    ap.add_argument("--cell-m", type=float, default=200.0)
    ap.add_argument("--radius-nm", type=float, default=6.0,
                    help="cells outside this radius from center are transparent")
    ap.add_argument("--max-agl-ft", type=float, default=2500.0,
                    help="drop track points above this AGL altitude")
    ap.add_argument("--local-only", action="store_true",
                    help="only process tracks whose first point is within "
                         "--local-radius-nm of the tracks.json center")
    ap.add_argument("--local-radius-nm", type=float, default=3.0,
                    help="radius for --local-only filter")
    ap.add_argument("--dt", type=float, default=1.0,
                    help="seconds between track samples")
    ap.add_argument("--influence-km", type=float, default=8.0)
    ap.add_argument("--floor-db", type=float, default=35.0,
                    help="cells below this are dropped from output")
    ap.add_argument("--vis-min-db", type=float, default=55.0,
                    help="(legacy) hard floor, ignored in adaptive mode")
    ap.add_argument("--vis-range-db", type=float, default=30.0,
                    help="max dB span of the color ramp below the observed peak")
    ap.add_argument("--png", default=None,
                    help="PNG output path (default: <out>.png)")
    args = ap.parse_args(argv)

    data = json.loads(Path(args.tracks).read_text())
    lat0, lon0 = data["center"]
    tracks = data.get("tracks") or []

    if args.local_only:
        import math
        m_per_deg_lat = 111_320.0
        m_per_deg_lon = 111_320.0 * math.cos(math.radians(lat0))
        radius_m = args.local_radius_nm * 1852.0
        r2 = radius_m * radius_m

        # Point-level clip. A BJC pattern aircraft that momentarily grazes
        # the 5 nm KBDU ring should contribute only its few crossing points,
        # not its entire southern pattern. We rewrite each track to contain
        # only the points that are actually inside the ring.
        clipped_tracks = []
        total_in, total_total = 0, 0
        for t in tracks:
            pts = t.get("points") or []
            total_total += len(pts)
            kept_pts = []
            for p in pts:
                dx = (p[1] - lon0) * m_per_deg_lon
                dy = (p[0] - lat0) * m_per_deg_lat
                if dx * dx + dy * dy <= r2:
                    kept_pts.append(p)
            total_in += len(kept_pts)
            if len(kept_pts) >= 2:
                clipped_tracks.append({**t, "points": kept_pts})
        print(f"local clip: {total_in}/{total_total} points kept, "
              f"{len(clipped_tracks)}/{len(tracks)} tracks have "
              f">=2 inside {args.local_radius_nm} nm")
        tracks = clipped_tracks

    grid = Grid(lat0=lat0, lon0=lon0,
                half_km=args.half_km, cell_m=args.cell_m)
    n = accumulate_tracks(grid, tracks, dt_s=args.dt,
                          influence_km=args.influence_km,
                          max_agl_ft=args.max_agl_ft)

    db = grid.to_db()
    # Adaptive ramp + soft alpha (matches the single-aircraft JS model).
    # Ramp spans the observed dB range inside the circular mask, capped
    # at vis_range_db so one ultra-hot cell can't wash out the rest.
    radius_m = args.radius_nm * 1852.0
    dist2 = grid.xs * grid.xs + grid.ys * grid.ys
    inside = dist2 <= radius_m * radius_m

    masked = np.where(np.isfinite(db) & inside, db, np.nan)
    if np.any(np.isfinite(masked)):
        db_max = float(np.nanmax(masked))
        db_min_obs = float(np.nanmin(masked))
    else:
        db_max = args.floor_db + 40.0
        db_min_obs = args.floor_db
    db_min = db_min_obs

    vis_max = db_max
    span = max(1.0, min(db_max - db_min_obs, args.vis_range_db))
    vis_min = vis_max - span

    t_raw = (np.where(np.isfinite(db), db, vis_min) - vis_min) / span
    t = np.clip(t_raw, 0.0, 1.0)
    t = np.flipud(t)
    inside_flip = np.flipud(inside)
    finite_flip = np.flipud(np.isfinite(db))

    r = np.where(t < 0.5, 0.0, (t - 0.5) * 2.0)
    g = np.where(t < 0.5, t * 2.0, 1.0 - (t - 0.5) * 2.0)
    b = np.where(t < 0.5, 1.0 - t * 2.0, 0.0)
    # Soft fade: 40 at the dim end -> 230 at the hot end. Outside the
    # circular mask and empty cells stay fully transparent.
    alpha = np.where(inside_flip & finite_flip, 40 + 190 * t, 0)

    rgba = np.stack([
        r * 255,
        g * 255,
        b * 255,
        alpha,
    ], axis=-1).astype(np.uint8)

    png_path = Path(args.png) if args.png else Path(args.out).with_suffix(".png")
    _write_png_rgba(str(png_path), rgba)

    out = {
        "center": [lat0, lon0],
        "bounds": grid.bounds(),
        "cell_m": args.cell_m,
        "n": grid.n,
        "floor_db": args.floor_db,
        "vis_min_db": vis_min,
        "vis_max_db": vis_max,
        "tracks_processed": n,
        "db_min": db_min,
        "db_max": db_max,
        "png": png_path.name,
    }
    Path(args.out).write_text(json.dumps(out))
    print(f"wrote {args.out} + {png_path}: {grid.n}x{grid.n} grid, "
          f"{n} tracks, peak {db_max} dB")
    return 0


def _write_png_rgba(path: str, rgba: np.ndarray) -> None:
    h, w, _ = rgba.shape
    raw = b"".join(b"\x00" + rgba[y].tobytes() for y in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    idat = zlib.compress(raw, 6)
    with open(path, "wb") as f:
        f.write(sig + chunk(b"IHDR", ihdr)
                + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


if __name__ == "__main__":
    sys.exit(main())
