# Next Data Refresh — Required Field Captures

## Capture timestamp + groundspeed per point

Current track points are 3-tuples `[lat, lon, alt]` (or 4-tuples with
`ts_ms` for live capture only). The next backfill / capture pass must
extend each point to include groundspeed, ideally also heading.

### Target shape
```
[lat, lon, alt_ft, ts_ms, gs_kt, heading_deg]
  0    1    2       3      4      5
```

### Why it matters
- **Accurate noise modeling**: `buildEnergyProfile` currently infers
  airspeed from position deltas, which is wrong for sparse ADS-B
  (5–10s gaps). Real `ac.gs` from the feed is exact.
- **Time-based densification**: noiseRaster's distance-based walker
  (current default 4 blobs/nm) doesn't need time, but the legacy
  time-based path (`targetBlobsPerMin`) and the intent classifier
  (`closure_pct`, `angular_accumulation`, etc.) do.
- **Phase + leg detection**: `/api/aircraft-ops` infers heading from
  consecutive position deltas; raw `ac.true_heading` or `ac.track`
  would be sharper than computed.

### Where to change

**Live capture worker** (`noise/web/capture-worker.js`, line ~195):
```js
// current
t.points.push([ac.lat, ac.lon, alt, Date.now()])
// new
t.points.push([ac.lat, ac.lon, alt, Date.now(), ac.gs, ac.true_heading || ac.track])
```

**Legacy live capture** (`noise/web/vite.config.js`, `liveCapturePlugin`,
search for `t.points.push`):
```js
t.points.push([ac.lat, ac.lon, alt, Date.now(), ac.gs, ac.true_heading || ac.track])
```

**Historical backfill** (`noise/import_globe_history.py`, search for
where points are built from globe.json archives):
- adsb.lol historical archives include `gs` and `track` per record.
  Update the per-point tuple to capture them.

### Consumers that benefit (need to handle 6-tuples gracefully)
- `noiseRaster.js` — `buildEnergyProfile` should prefer `p[4]` (real gs)
  over computed velocity
- `vite.config.js` `/api/aircraft-ops` — use `p[4]`/`p[5]` for closure
  and heading rather than deltas
- `DescentTest.jsx` — same
- `bandTrack` / classification — already handles variable-length tuples
  via `p.length > 3` checks

### Backward compatibility
All consumers must keep handling 3-tuple and 4-tuple legacy data
(`p.length` check). Don't break the 130k existing historical tracks.

### Order of operations
1. Update capture-worker.js (live data starts capturing immediately)
2. Update vite.config.js liveCapturePlugin (local dev capture)
3. Deploy to Railway → live_tracks table starts accumulating 6-tuples
4. Update import_globe_history.py
5. Re-run historical backfill (or just accept that pre-2026 data lacks
   speed)

### Verification
After deploy:
```bash
curl 'https://web-app-production-fedf.up.railway.app/api/excursions/boot?hours=0.25&limit=1' \
  | jq '.tracks[0].bands[0].points[0] | length'
# expect: 6
```
