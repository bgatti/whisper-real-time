"""Extract a single capture-date slice of tracks_yearly.json for quick tests.

Usage:
  python -m noise.slice_day --date 2024-10-15 \
    --out noise/web/public/tracks_day.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="noise/web/public/tracks_yearly.json")
    ap.add_argument("--date", required=True,
                    help="capture date like 2024-10-15 (matches src field)")
    ap.add_argument("--out", default="noise/web/public/tracks_day.json")
    args = ap.parse_args()

    d = json.loads(Path(args.source).read_text())
    kept = [t for t in d.get("tracks", [])
            if f"/{args.date}/" in (t.get("src") or "")]

    out = {
        "center": d.get("center"),
        "radius_nm": d.get("radius_nm"),
        "alt_max_ft": d.get("alt_max_ft"),
        "capture_date": args.date,
        "tracks": kept,
    }
    Path(args.out).write_text(json.dumps(out, separators=(",", ":")))
    pts = sum(len(t.get("points", [])) for t in kept)
    print(f"wrote {args.out}: {len(kept)} tracks, {pts} points for {args.date}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
