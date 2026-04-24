"""Build a heatmap PNG for a single aircraft by tail number."""

from __future__ import annotations

import argparse
import json
import struct
import sys
import zlib
from pathlib import Path

import numpy as np

from .noise_heatmap import Grid, accumulate_track


def _write_png_rgba(path: str, rgba: np.ndarray) -> None:
    h, w, _ = rgba.shape
    raw = b"".join(b"\x00" + rgba[y].tobytes() for y in range(h))
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 6))
                + chunk(b"IEND", b""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tail", required=True)
    ap.add_argument("--half-km", type=float, default=8.0)
    ap.add_argument("--cell-m", type=float, default=120.0)
    ap.add_argument("--max-agl-ft", type=float, default=2500.0)
    ap.add_argument("--out-dir", default="noise/debug_heatmaps")
    ap.add_argument("--sources", nargs="+", default=[
        "noise/web/public/tracks.json",
        "noise/web/public/tracks_yearly.json",
        "noise/web/public/tracks_historical.json",
    ])
    args = ap.parse_args()

    hits = []
    for src in args.sources:
        p = Path(src)
        if not p.exists():
            continue
        d = json.loads(p.read_text())
        for t in d.get("tracks", []):
            if t.get("call") == args.tail:
                hits.append((src, t))

    if not hits:
        print(f"no tracks for tail {args.tail}")
        return 1

    print(f"found {len(hits)} track(s) for {args.tail}")
    src0, t0 = hits[0]
    pts = t0.get("points") or []
    icao_type = t0.get("type", "")
    print(f"using {src0} {t0.get('src','')} type={icao_type} pts={len(pts)}")

    # center grid on track centroid
    lat0 = sum(p[0] for p in pts) / len(pts)
    lon0 = sum(p[1] for p in pts) / len(pts)

    # run all five stages for this one aircraft
    stages = [
        ("single_stage1_hp_only", dict(use_spread=False, use_altitude=False,
                                        use_ground=False, use_directivity=False)),
        ("single_stage2_horiz",   dict(use_spread=True, use_altitude=False,
                                        use_ground=False, use_directivity=False)),
        ("single_stage3_slant",   dict(use_spread=True, use_altitude=True,
                                        use_ground=False, use_directivity=False)),
        ("single_stage4_ground",  dict(use_spread=True, use_altitude=True,
                                        use_ground=True, use_directivity=False)),
        ("single_stage5_full",    dict(use_spread=True, use_altitude=True,
                                        use_ground=True, use_directivity=True)),
    ]

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    for name, flags in stages:
        grid = Grid(lat0=lat0, lon0=lon0,
                    half_km=args.half_km, cell_m=args.cell_m)
        for _, t in hits:
            accumulate_track(grid, t.get("points") or [], t.get("type", ""),
                             dt_s=1.0, influence_km=3.0,
                             max_agl_ft=args.max_agl_ft, **flags)
        db = grid.to_db()
        finite = db[np.isfinite(db)]
        if finite.size == 0:
            print(f"{name}: no energy")
            continue
        dbmax = float(np.max(finite))
        p = np.percentile(finite, [50, 75, 90, 95, 99])
        vis_max = dbmax
        vis_min = dbmax - 25.0
        span = max(1.0, vis_max - vis_min)

        t_norm = np.clip((np.where(np.isfinite(db), db, vis_min) - vis_min) / span,
                         0.0, 1.0)
        t_norm = np.flipud(t_norm)
        r = np.where(t_norm < 0.5, 0.0, (t_norm - 0.5) * 2.0)
        g = np.where(t_norm < 0.5, t_norm * 2.0, 1.0 - (t_norm - 0.5) * 2.0)
        b = np.where(t_norm < 0.5, 1.0 - t_norm * 2.0, 0.0)
        alpha = np.where(np.isfinite(np.flipud(db)) & (np.flipud(db) > vis_min),
                         255, 0)
        rgba = np.stack([r * 255, g * 255, b * 255, alpha], axis=-1).astype(np.uint8)
        png = out_dir / f"{args.tail}_{name}.png"
        _write_png_rgba(str(png), rgba)
        print(f"{name:28s} 50={p[0]:5.1f} 75={p[1]:5.1f} 95={p[3]:5.1f} "
              f"99={p[4]:5.1f} max={dbmax:5.1f}  -> {png}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
