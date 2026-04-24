"""
Prepend a 2026 (year-0) stub row to flights_yearly.json so the YoY table shows it.
Trajectory KPIs will come from globe_history; OpenSky metadata fill in tomorrow.
"""
import json, os, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
META = os.path.join(HERE, "web", "public", "flights_yearly.json")

with open(META) as fh:
    data = json.load(fh)

# Skip if already has year-0
if any(y.get("years_back") == 0 for y in data["years"]):
    print("2026 row already present — nothing to do")
else:
    today = datetime.date.today()
    week_ago = today - datetime.timedelta(days=7)
    data["years"].insert(0, {
        "years_back": 0,
        "window_end": today.isoformat(),
        "window_begin": week_ago.isoformat(),
        "arrivals": None,
        "departures": None,
        "unique_flights": None,
        "unique_aircraft": None,
        "flights": [],
        "_pending": "OpenSky metadata cooldown until ~23:22 local",
    })
    with open(META, "w") as fh:
        json.dump(data, fh, separators=(",", ":"))
    print(f"added 2026 stub (window {week_ago} .. {today})")
