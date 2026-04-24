"""Build several heatmap PNGs that isolate one piece of the model at a time.

Each stage turns on one more piece of the math. Compare the PNGs side by
side to see which step changes the footprint in the expected way.

  stage1_hp_only   : HP + power state only. Every cell in the influence
                     radius gets the full source level. Shows raw traffic
                     density (how many sample points "cover" each cell).
  stage2_horiz     : + horizontal-only spreading. Aircraft pinned to ground
                     (altitude = 0), no ground/graze/atm, no directivity.
  stage3_slant     : + real AGL altitude in the slant range (still no
                     ground/graze/atm, no directivity).
  stage4_ground    : + ground + grazing + atmospheric losses.
  stage5_full      : + propeller directivity (final production model).

All stages share the same grid, colormap, and 6 nm mask — only the math
differs. Outputs go to noise/debug_heatmaps/.
"""

from __future__ import annotations

import json
import struct
import sys
import zlib
from pathlib import Path

import numpy as np

from .noise_heatmap import Grid, accumulate_tracks


STAGES = [
    ("stage1_hp_only",
     dict(use_spread=False, use_altitude=False,
          use_ground=False, use_directivity=False)),
    ("stage2_horiz",
     dict(use_spread=True, use_altitude=False,
          use_ground=False, use_directivity=False)),
    ("stage3_slant",
     dict(use_spread=True, use_altitude=True,
          use_ground=False, use_directivity=False)),
    ("stage4_ground",
     dict(use_spread=True, use_altitude=True,
          use_ground=True, use_directivity=False)),
    ("stage5_full",
     dict(use_spread=True, use_altitude=True,
          use_ground=True, use_directivity=True)),
]


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


def colorize(db: np.ndarray, inside: np.ndarray,
             vis_min: float, vis_max: float) -> np.ndarray:
    span = max(1.0, vis_max - vis_min)
    t = np.clip((np.where(np.isfinite(db), db, vis_min) - vis_min) / span,
                0.0, 1.0)
    t = np.flipud(t)
    inside_f = np.flipud(inside)
    r = np.where(t < 0.5, 0.0, (t - 0.5) * 2.0)
    g = np.where(t < 0.5, t * 2.0, 1.0 - (t - 0.5) * 2.0)
    b = np.where(t < 0.5, 1.0 - t * 2.0, 0.0)
    alpha = np.where(inside_f, 255, 0)
    return np.stack([r * 255, g * 255, b * 255, alpha], axis=-1).astype(np.uint8)


def main() -> int:
    src = Path("noise/web/public/tracks.json")
    out_dir = Path("noise/debug_heatmaps")
    out_dir.mkdir(parents=True, exist_ok=True)

    data = json.loads(src.read_text())
    lat0, lon0 = data["center"]
    tracks = data.get("tracks") or []

    half_km = 12.0
    cell_m = 200.0
    max_agl_ft = 2500.0
    radius_nm = 6.0
    vis_min = 55.0

    summary = []
    for name, flags in STAGES:
        grid = Grid(lat0=lat0, lon0=lon0, half_km=half_km, cell_m=cell_m)
        accumulate_tracks(grid, tracks, dt_s=1.0, influence_km=3.0,
                          max_agl_ft=max_agl_ft, **flags)
        db = grid.to_db()

        R = radius_nm * 1852.0
        inside = (grid.xs * grid.xs + grid.ys * grid.ys) <= R * R
        finite = db[np.isfinite(db) & inside]
        if finite.size:
            p = np.percentile(finite, [50, 75, 90, 95, 99, 100])
            vis_max = float(p[-1])
        else:
            p = [0] * 6
            vis_max = vis_min + 1
        rgba = colorize(db, inside, vis_min=vis_min, vis_max=vis_max)
        png_path = out_dir / f"{name}.png"
        _write_png_rgba(str(png_path), rgba)

        summary.append({
            "stage": name,
            "flags": flags,
            "vis_min": vis_min,
            "vis_max": vis_max,
            "pct_50": round(float(p[0]), 1),
            "pct_75": round(float(p[1]), 1),
            "pct_90": round(float(p[2]), 1),
            "pct_95": round(float(p[3]), 1),
            "pct_99": round(float(p[4]), 1),
            "max": round(float(p[5]), 1),
            "png": str(png_path),
        })
        print(f"{name:16s}  50={p[0]:5.1f}  75={p[1]:5.1f}  "
              f"95={p[3]:5.1f}  99={p[4]:5.1f}  max={p[5]:5.1f}")

    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
