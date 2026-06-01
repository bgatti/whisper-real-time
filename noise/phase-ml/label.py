"""Real-time flight labelling CLI.

Driven by the flight-labeler agent. Two modes:

  1. inspect   — load a flight (live or archived), run classifier, print
                 a numbered list of events the labeller can pick from.
  2. label     — append a human-confirmed (or corrected) label for one event
                 to data/labels.jsonl.

Examples:
  python label.py inspect --source live --tail N4593Y
  python label.py inspect --source archive --year 2026 --tail N265SF
  python label.py inspect --source file --path C:/tmp/some_track.json
  python label.py label  --event-id <id>  --label landed_full_stop  --labeler ben
  python label.py label  --event-id <id>  --label touch_and_go     --notes "ATC said go-around"

Events are deterministically identified by:
    {icao_hex}-{event_start_epoch_ms}-{event_type}
so the same event always gets the same id, and re-labelling overwrites cleanly.

Labels are appended to noise/phase-ml/data/labels.jsonl (one JSON per line).
That file is the training set for the eventual XGBoost model — see kickoff doc.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).parent))

from phase_ml import airports as A
from phase_ml import data_loader, features, intent, maneuvers, oracle
from phase_ml.data_loader import Point, Track

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

LABELS_DIR = Path(__file__).parent / "data"
LABELS_FILE = LABELS_DIR / "labels.jsonl"

# Valid label set — strict whitelist so typos don't silently corrupt training data.
# Mirrors the maneuver detector outputs + the oracle's phase labels.
VALID_LABELS = {
    # Outcome of a landing event
    "touch_and_go", "landed_full_stop", "go_around",
    # PTS maneuvers
    "steep_turn", "s_turns_across_road", "turn_around_a_point",
    "chandelle", "lazy_8", "slow_flight", "stall_recovery",
    "emergency_descent", "holding_pattern", "thermalling",
    "sightseeing_orbit",
    # Oracle phases (per-window, not per-event)
    "on_ground", "taxiing", "pattern", "practice_area",
    "departing", "inbound", "en_route", "nearby",
    # Special
    "false_positive",     # the classifier fired but nothing real happened
    "ambiguous",           # human reviewed and can't decide
    "skip",                # not enough data to label
}


# ---------------------------------------------------------------------------
# Source loading
# ---------------------------------------------------------------------------

def load_track_archive(year: int, tail: str | None) -> Track | None:
    """Return the longest matching track in the archive for `year`."""
    if tail:
        tracks = data_loader.find_tracks(year=year, tail=tail, min_points=10, max_returns=10)
        if not tracks:
            return None
        return max(tracks, key=lambda t: len(t))
    # No tail given: return the longest track in the file
    longest: Track | None = None
    for t in data_loader.iter_tracks(year=year):
        if longest is None or len(t) > len(longest):
            longest = t
    return longest


def load_track_live(path: Path, tail: str | None) -> Track | None:
    """Load from `noise/web/public/tracks_live.json` (live capture buffer).

    The live file's schema is the same nested per-aircraft structure as the
    archive year files; we look up by tail or take the most-recent tail.
    """
    if not path.exists():
        raise FileNotFoundError(f"live tracks file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    tracks_raw = data.get("tracks") or data.get("aircraft") or []
    candidates: list[Track] = []
    for raw in tracks_raw:
        # Live file may put points under 'points' or 'positions'; normalise.
        if "points" not in raw and "positions" in raw:
            raw["points"] = raw["positions"]
        try:
            t = data_loader.load_track(raw)
        except Exception:
            continue
        if tail and t.tail.upper() != tail.upper():
            continue
        candidates.append(t)
    if not candidates:
        return None
    # Pick the one with the most recent last fix (most-current activity)
    return max(candidates, key=lambda t: t.points[-1].ts_unix if t.points else 0)


def load_track_file(path: Path) -> Track:
    """Load a single track from any JSON file matching the archive item schema."""
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    if "tracks" in raw and raw["tracks"]:
        return data_loader.load_track(raw["tracks"][0])
    return data_loader.load_track(raw)


# ---------------------------------------------------------------------------
# Event extraction
# ---------------------------------------------------------------------------

def event_id(icao_hex: str, start_ts: float, type_: str) -> str:
    return f"{icao_hex}-{int(start_ts*1000)}-{type_}"


def build_events(track: Track) -> list[dict]:
    """Run the classifier and return a list of labellable events.

    Each event is a dict with id, type, predicted_confidence, time window,
    explanation, and the evidence dict the detector built.

    Includes:
      - Every maneuver detection
      - The current intent prediction at the LAST sample (so the labeller can
        confirm "yes this aircraft is inbound to KBJC")
    """
    samples = features.enrich(track.points)
    events: list[dict] = []

    # Maneuvers
    for d in maneuvers.detect_all(samples, type_code=track.type_code):
        events.append({
            "id": event_id(track.icao_hex, d.start_ts, d.type),
            "kind": "maneuver",
            "predicted_type": d.type,
            "predicted_confidence": round(d.confidence, 3),
            "start_ts": d.start_ts,
            "end_ts": d.end_ts,
            "duration_s": d.duration_s,
            "explanation": d.explanation,
            "evidence": _clean_evidence(d.evidence),
        })

    # Intent at last sample (3-min window)
    if samples:
        end_ts = samples[-1].point.ts_unix
        start_idx = 0
        for i in range(len(samples) - 1, -1, -1):
            if end_ts - samples[i].point.ts_unix >= 180.0:
                start_idx = i
                break
        window = features.build_window(samples[start_idx:])
        summary = intent.summarise(window)
        airport_str = summary.top.airport or "transit"
        events.append({
            "id": event_id(track.icao_hex, end_ts, f"intent_{airport_str}"),
            "kind": "intent",
            "predicted_type": f"inbound_{airport_str}" if summary.top.airport else "transit",
            "predicted_confidence": round(summary.top.probability, 3),
            "start_ts": window.point_start.ts_unix if window.point_start else end_ts,
            "end_ts": end_ts,
            "duration_s": 180.0,
            "explanation": summary.top.explanation,
            "evidence": {
                "airport": summary.top.airport,
                "runway": summary.top.runway,
                "runner_up": (summary.runner_up.airport if summary.runner_up else None),
                "confidence_gap": round(summary.confidence_gap, 3),
                "all_ranked": [
                    {"airport": s.airport, "runway": s.runway, "p": round(s.probability, 3)}
                    for s in summary.all_scores[:5]
                ],
            },
        })

    events.sort(key=lambda e: e["start_ts"])
    return events


def _clean_evidence(ev: dict) -> dict:
    """Drop non-JSON-serialisable bits and round floats for readability."""
    out: dict = {}
    for k, v in ev.items():
        if isinstance(v, float):
            out[k] = round(v, 3)
        elif isinstance(v, (int, str, bool)) or v is None:
            out[k] = v
        elif isinstance(v, (list, tuple)):
            out[k] = [_clean_evidence(x) if isinstance(x, dict) else x for x in v]
        elif isinstance(v, dict):
            out[k] = _clean_evidence(v)
        else:
            out[k] = str(v)
    return out


# ---------------------------------------------------------------------------
# Pretty-printing
# ---------------------------------------------------------------------------

def print_inspect(track: Track, events: list[dict], *, json_output: bool) -> None:
    if json_output:
        payload = {
            "track": {
                "tail": track.tail,
                "icao_hex": track.icao_hex,
                "type_code": track.type_code,
                "owner_operator": track.owner_operator,
                "date": track.date,
                "n_points": len(track.points),
                "duration_min": round(track.duration_s() / 60, 1),
            },
            "events": events,
        }
        print(json.dumps(payload, indent=2))
        return

    print("=" * 100)
    print(f"  {track.tail}  ({track.type_code})  {track.owner_operator}")
    print(f"  ICAO: {track.icao_hex}  date: {track.date}  {len(track.points)} pts, {track.duration_s()/60:.1f} min")
    print(f"  events: {len(events)}")
    print()
    for i, e in enumerate(events, 1):
        ts_local = dt.datetime.fromtimestamp(e["start_ts"], dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        print(f"  [{i:>3}]  {e['kind']:<8s}  {e['predicted_type']:<22s} "
              f"conf={e['predicted_confidence']:.2f}  {ts_local}  dur={e['duration_s']:.0f}s")
        print(f"         id: {e['id']}")
        print(f"         {e['explanation']}")
        if e["kind"] == "intent":
            for r in e["evidence"]["all_ranked"]:
                print(f"            P={r['p']:.2f}  airport={r['airport']}  runway={r['runway']}")
        print()


# ---------------------------------------------------------------------------
# Label writing
# ---------------------------------------------------------------------------

def write_label(
    *,
    event_id_str: str,
    label: str,
    labeler: str,
    notes: str = "",
    predicted_type: str | None = None,
    predicted_confidence: float | None = None,
    extra: dict | None = None,
) -> dict:
    if label not in VALID_LABELS:
        raise ValueError(f"label '{label}' not in VALID_LABELS; "
                         f"add it to label.py if you mean it on purpose")
    LABELS_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "event_id": event_id_str,
        "label": label,
        "labeler": labeler,
        "label_ts_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "predicted_type": predicted_type,
        "predicted_confidence": predicted_confidence,
        "notes": notes,
        **(extra or {}),
    }
    with LABELS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    return record


def read_labels_for_event(event_id_str: str) -> list[dict]:
    if not LABELS_FILE.exists():
        return []
    out: list[dict] = []
    with LABELS_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("event_id") == event_id_str:
                out.append(rec)
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Real-time flight labeller (driven by the flight-labeler agent).")
    sub = p.add_subparsers(dest="cmd", required=True)

    insp = sub.add_parser("inspect", help="Load a flight, classify, print labellable events.")
    insp.add_argument("--source", choices=("live", "archive", "file"), required=True)
    insp.add_argument("--year", type=int, default=dt.datetime.now().year)
    insp.add_argument("--tail", type=str, default=None)
    insp.add_argument("--path", type=str, default=None,
                      help="for --source file: path to a single track JSON")
    insp.add_argument("--live-path", type=str,
                      default=str(Path(__file__).resolve().parent.parent / "web" / "public" / "tracks_live.json"))
    insp.add_argument("--json", action="store_true", help="emit JSON instead of human format")

    lbl = sub.add_parser("label", help="Record a label for an event id.")
    lbl.add_argument("--event-id", required=True)
    lbl.add_argument("--label", required=True, choices=sorted(VALID_LABELS))
    lbl.add_argument("--labeler", required=True, help="who is labelling (your email/handle)")
    lbl.add_argument("--notes", default="")
    lbl.add_argument("--predicted-type", default=None,
                     help="what the classifier predicted (copy from inspect output)")
    lbl.add_argument("--predicted-confidence", type=float, default=None)

    sub.add_parser("labels", help="Print all stored labels.")
    return p


def main() -> int:
    args = build_parser().parse_args()
    if args.cmd == "inspect":
        track = _load_track_for(args)
        if track is None:
            print(f"no track found (source={args.source} tail={args.tail})", file=sys.stderr)
            return 2
        events = build_events(track)
        print_inspect(track, events, json_output=args.json)
        return 0
    if args.cmd == "label":
        rec = write_label(
            event_id_str=args.event_id,
            label=args.label,
            labeler=args.labeler,
            notes=args.notes,
            predicted_type=args.predicted_type,
            predicted_confidence=args.predicted_confidence,
        )
        print(json.dumps(rec, indent=2))
        return 0
    if args.cmd == "labels":
        if not LABELS_FILE.exists():
            print("no labels file yet")
            return 0
        with LABELS_FILE.open("r", encoding="utf-8") as f:
            sys.stdout.write(f.read())
        return 0
    return 1


def _load_track_for(args) -> Track | None:
    if args.source == "live":
        return load_track_live(Path(args.live_path), tail=args.tail)
    if args.source == "archive":
        return load_track_archive(year=args.year, tail=args.tail)
    if args.source == "file":
        if not args.path:
            raise SystemExit("--path is required for --source file")
        return load_track_file(Path(args.path))
    raise SystemExit(f"unknown source: {args.source}")


if __name__ == "__main__":
    raise SystemExit(main())
