"""Load tracks from the on-disk ADS-B archive.

The archive lives at  C:/tmp/noise_data/tracks_YYYY.json  and looks like:

    {
        "year": "2026",
        "center": [lat, lon],
        "region": {lat_min, lat_max, lon_min, lon_max, den_exclusion_nm},
        "alt_max_ft": 9000,
        "tracks": [
            {
                "call": "N87367",     # tail
                "type": "E75L",       # ICAO type code
                "desc": "...",
                "ownOp": "...",
                "src":  "globe/2026-04-10/ac04c5",   # ICAO hex is the last path segment
                "t0":   1775779200.0,                # epoch seconds; points have offsets
                "points": [
                    [lat, lon, alt_msl_ft, t_offset_seconds],
                    ...
                ],
                "year":       "2026",
                "years_back": 0
            },
            ...
        ]
    }

This module normalises the raw schema into a typed `Track` and a list of `Point`s
with absolute UTC timestamps.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Sequence

DEFAULT_ARCHIVE_DIR = Path("C:/tmp/noise_data")


@dataclass
class Point:
    lat: float
    lon: float
    alt_msl_ft: float
    ts_unix: float    # absolute UTC seconds

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.lat, self.lon, self.alt_msl_ft, self.ts_unix)


@dataclass
class Track:
    tail: str
    icao_hex: str
    type_code: str
    description: str
    owner_operator: str
    date: str                 # YYYY-MM-DD parsed from src path
    t0: float                 # epoch seconds (for reference; points already absolute)
    points: list[Point] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.points)

    def duration_s(self) -> float:
        if len(self.points) < 2:
            return 0.0
        return self.points[-1].ts_unix - self.points[0].ts_unix

    def bounding_box(self) -> tuple[float, float, float, float]:
        lats = [p.lat for p in self.points]
        lons = [p.lon for p in self.points]
        return min(lats), max(lats), min(lons), max(lons)


def _parse_meta(raw: dict) -> tuple[str, str]:
    """Extract (date, icao_hex) from the 'src' field, which looks like 'globe/2026-04-10/ac04c5'."""
    src = raw.get("src", "")
    parts = src.split("/")
    date = parts[1] if len(parts) >= 2 else ""
    icao_hex = parts[-1] if parts else ""
    return date, icao_hex


def _decode_points(raw_pts: Sequence[Sequence[float]], t0: float) -> list[Point]:
    """Convert raw 4-tuples to Point objects with absolute timestamps.

    The 4th value is seconds since t0; convert by adding t0 (epoch seconds).
    """
    out: list[Point] = []
    for p in raw_pts:
        if len(p) < 4:
            continue
        lat, lon, alt, t_off = p[0], p[1], p[2], p[3]
        if lat is None or lon is None:
            continue
        out.append(Point(lat=float(lat), lon=float(lon),
                         alt_msl_ft=float(alt),
                         ts_unix=float(t0) + float(t_off)))
    return out


def load_track(raw: dict) -> Track:
    date, icao_hex = _parse_meta(raw)
    t0 = float(raw.get("t0", 0))
    points = _decode_points(raw.get("points") or [], t0)
    return Track(
        tail=raw.get("call") or "",
        icao_hex=icao_hex,
        type_code=raw.get("type") or "",
        description=raw.get("desc") or "",
        owner_operator=raw.get("ownOp") or "",
        date=date,
        t0=t0,
        points=points,
    )


def iter_tracks(year: int = 2026, archive_dir: Path | str = DEFAULT_ARCHIVE_DIR) -> Iterator[Track]:
    """Yield every Track in the year file. ~19K for 2026."""
    archive_dir = Path(archive_dir)
    path = archive_dir / f"tracks_{year}.json"
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    for raw in data.get("tracks", []):
        try:
            yield load_track(raw)
        except (KeyError, TypeError, ValueError):
            continue


def load_all_tracks(year: int = 2026, archive_dir: Path | str = DEFAULT_ARCHIVE_DIR) -> list[Track]:
    return list(iter_tracks(year=year, archive_dir=archive_dir))


def find_tracks(
    *,
    year: int = 2026,
    tail: str | None = None,
    type_code: str | None = None,
    min_points: int = 0,
    max_returns: int | None = None,
    archive_dir: Path | str = DEFAULT_ARCHIVE_DIR,
) -> list[Track]:
    """Light filter over the archive. Useful for the demo and tests."""
    out: list[Track] = []
    for t in iter_tracks(year=year, archive_dir=archive_dir):
        if tail and t.tail.upper() != tail.upper():
            continue
        if type_code and t.type_code.upper() != type_code.upper():
            continue
        if len(t) < min_points:
            continue
        out.append(t)
        if max_returns is not None and len(out) >= max_returns:
            break
    return out
