"""End-to-end demo of the phase_ml package.

Usage:
    python demo.py                       # pick a long track from 2026 and classify it
    python demo.py --tail N4593Y         # specific tail
    python demo.py --year 2026 --top 5   # show top-N longest tracks
    python demo.py --tail N4593Y --window-end-min 90  # intent at t = 90 min into track

The demo:
    1. Picks a track from C:/tmp/noise_data/tracks_<year>.json
    2. Enriches it (computes gs/vs/track per fix)
    3. Runs the oracle to produce a per-sample phase label and a distribution summary
    4. Runs every maneuver detector and prints the catch
    5. Runs the intent predictor at the final sample and shows the ranked airport hypotheses
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

# Force UTF-8 so block-characters / unicode in output don't blow up on Windows cp1252.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # py3.7+
except AttributeError:
    pass

# When run as a script: ensure the local package is importable without install.
sys.path.insert(0, str(Path(__file__).parent))

from phase_ml import airports as A
from phase_ml import data_loader, features, intent, maneuvers, oracle


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--year", type=int, default=2026)
    p.add_argument("--tail", type=str, default=None)
    p.add_argument("--type", type=str, default=None, help="filter by ICAO type code (e.g. C172, GLID)")
    p.add_argument("--min-points", type=int, default=200)
    p.add_argument("--top", type=int, default=1, help="when no --tail given, show top-N longest tracks")
    p.add_argument("--archive-dir", type=str, default="C:/tmp/noise_data")
    p.add_argument("--window-end-min", type=float, default=None,
                   help="evaluate intent at this many minutes into the track (default: last sample)")
    p.add_argument("--window-len-min", type=float, default=3.0,
                   help="intent window length in minutes")
    return p.parse_args()


def pick_tracks(args) -> list[data_loader.Track]:
    if args.tail:
        return data_loader.find_tracks(
            year=args.year, tail=args.tail, min_points=args.min_points,
            archive_dir=args.archive_dir,
        )
    # Stream and pick top-N longest matching the type filter.
    print(f"Scanning {args.year} archive for {'all types' if not args.type else 'type=' + args.type}…")
    heap: list[data_loader.Track] = []
    for t in data_loader.iter_tracks(year=args.year, archive_dir=args.archive_dir):
        if args.type and t.type_code.upper() != args.type.upper():
            continue
        if len(t) < args.min_points:
            continue
        heap.append(t)
    heap.sort(key=lambda t: -len(t))
    return heap[:args.top]


def show_phase_distribution(labels) -> None:
    counts = Counter(L.phase for L in labels)
    total = sum(counts.values())
    print("\n  Phase distribution (oracle):")
    for phase, n in counts.most_common():
        pct = 100 * n / total
        bar = "#" * int(pct / 2)
        print(f"    {phase:<20s} {n:>5d} ({pct:5.1f}%) {bar}")


def show_maneuver_catch(detections) -> None:
    print(f"\n  Maneuvers detected: {len(detections)}")
    if not detections:
        print("    (none)")
        return
    for d in detections:
        ts = d.start_ts
        print(f"    [{int(ts) % 100_000:>5d}s] {d}")


def show_intent(samples, *, end_idx: int, window_len_s: float, prior_tail: str = "") -> None:
    # Choose the trailing window ending at end_idx.
    if end_idx < 0 or end_idx >= len(samples):
        end_idx = len(samples) - 1
    end_ts = samples[end_idx].point.ts_unix
    start_idx = end_idx
    while start_idx > 0 and end_ts - samples[start_idx].point.ts_unix < window_len_s:
        start_idx -= 1
    window = features.build_window(samples[start_idx:end_idx + 1])

    # Look up the home airport (if any) from the fleet — small bonus prior.
    prior = None
    # We don't have the fleet at hand here, so we'll let the user supply this via CLI later.

    summary = intent.summarise(window, prior_by_airport=prior)
    print(f"\n  Intent at t={int(end_ts) % 100_000}s (window len {window_len_s:.0f}s):")
    print(f"    aircraft at lat={window.point_end.lat:.4f}, lon={window.point_end.lon:.4f}, alt={window.point_end.alt_msl_ft:.0f} MSL")
    print(f"    avg GS {window.gs_mean_kts:.0f} kt, avg VS {window.vs_mean_fpm:.0f} fpm, abs turn {window.abs_turn_total_deg:.0f}°")
    if summary.top.airport:
        print(f"    BEST: inbound to {summary.top.airport} runway {summary.top.runway or '?'}  P={summary.top.probability:.2f}  (gap to #2: {summary.confidence_gap:.2f})")
    else:
        print(f"    BEST: transit / unclassified  P={summary.top.probability:.2f}")
    print(f"    {summary.top.explanation}")
    print(f"\n    Full ranking:")
    for s in summary.all_scores:
        print(f"      {s}")


def classify_one(track: data_loader.Track, args) -> None:
    print("=" * 100)
    print(f"  Tail: {track.tail}  ICAO: {track.icao_hex}  Type: {track.type_code}  "
          f"({track.description})")
    print(f"  Operator: {track.owner_operator}")
    print(f"  Date: {track.date}  Points: {len(track)}  Duration: {track.duration_s() / 60:.1f} min")

    samples = features.enrich(track.points)
    print(f"  GS range {min(s.gs_kts for s in samples):.0f}-{max(s.gs_kts for s in samples):.0f} kt, "
          f"alt range {min(s.point.alt_msl_ft for s in samples):.0f}-{max(s.point.alt_msl_ft for s in samples):.0f} ft")

    # Oracle pass (nearest airport per sample)
    labels = oracle.classify_track(track)
    show_phase_distribution(labels)

    # Maneuver pass
    detections = maneuvers.detect_all(samples, type_code=track.type_code)
    show_maneuver_catch(detections)

    # Intent pass
    if args.window_end_min is not None:
        target_ts = samples[0].point.ts_unix + args.window_end_min * 60.0
        end_idx = max(0, next((i for i, s in enumerate(samples) if s.point.ts_unix >= target_ts), len(samples) - 1) - 1)
    else:
        end_idx = len(samples) - 1
    show_intent(samples, end_idx=end_idx, window_len_s=args.window_len_min * 60.0)
    print()


def main() -> None:
    args = parse_args()
    tracks = pick_tracks(args)
    if not tracks:
        sys.exit("no matching tracks found")
    for t in tracks:
        classify_one(t, args)


if __name__ == "__main__":
    main()
