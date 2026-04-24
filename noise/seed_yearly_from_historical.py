"""
Stub: build tracks_yearly.json from whatever we already have.

Right now that's tracks_historical.json (15 tracks from last week) tagged as
the current year, so the Year-over-Year KPI page has something to chew on while
we figure out a better way to backfill /tracks/all (which is currently 429'd).
"""
import json, os

HERE = os.path.dirname(os.path.abspath(__file__))
HIST = os.path.join(HERE, "web", "public", "tracks_historical.json")
META = os.path.join(HERE, "web", "public", "flights_yearly.json")
OUT = os.path.join(HERE, "web", "public", "tracks_yearly.json")

with open(HIST) as fh:
    hist = json.load(fh)

# Tag the stub tracks with the most-recent year from the metadata so the
# YoY page's table join succeeds. This is a fudge for demo purposes.
with open(META) as fh:
    meta = json.load(fh)
latest = min(meta["years"], key=lambda y: y["years_back"])
year = latest["window_end"][:4]
tracks = []
for t in hist.get("tracks", []):
    tracks.append({**t, "year": year, "years_back": 1})

payload = {
    "center": hist.get("center"),
    "radius_nm": hist.get("radius_nm"),
    "alt_max_ft": hist.get("alt_max_ft"),
    "by_year": {year: {"years_back": 1, "count": len(tracks)}},
    "tracks": tracks,
    "_note": "Stub — populated from tracks_historical.json until /tracks/all is unblocked.",
}

with open(OUT, "w") as fh:
    json.dump(payload, fh, separators=(",", ":"))
print(f"wrote {len(tracks)} tracks (year {year}) -> {OUT}")
