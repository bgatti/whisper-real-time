"""
Rank aircraft by how often they appeared at KBDU across the 5 yearly samples
in flights_yearly.json. Useful as a priority list for targeted trajectory
backfills from adsb.lol globe_history.
"""
import json, os, collections

HERE = os.path.dirname(os.path.abspath(__file__))
META = os.path.join(HERE, "web", "public", "flights_yearly.json")

with open(META) as fh:
    meta = json.load(fh)

by_hex = collections.Counter()
by_hex_years = collections.defaultdict(set)
calls_by_hex = collections.defaultdict(set)
for y in meta["years"]:
    yr = y["window_end"][:4]
    for f in y["flights"]:
        hex_id = f.get("icao24")
        if not hex_id:
            continue
        by_hex[hex_id] += 1
        by_hex_years[hex_id].add(yr)
        if f.get("call"):
            calls_by_hex[hex_id].add(f["call"])

rows = []
for hex_id, n in by_hex.most_common(40):
    rows.append({
        "hex": hex_id,
        "count": n,
        "years": sorted(by_hex_years[hex_id]),
        "calls": sorted(calls_by_hex[hex_id])[:4],
    })

print(f"{'hex':<8} {'n':>4}  {'years':<30} calls")
for r in rows:
    yrs = ",".join(y[-2:] for y in r["years"])
    calls = " ".join(r["calls"])
    print(f"{r['hex']:<8} {r['count']:>4}  {yrs:<30} {calls}")

# save
with open(os.path.join(HERE, "aircraft_ranked.json"), "w") as fh:
    json.dump(rows, fh, indent=2)
print(f"\nsaved top {len(rows)} -> noise/aircraft_ranked.json")
