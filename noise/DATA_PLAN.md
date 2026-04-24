# Data plan — evening of 2026-04-15

## Open improvement: timestamps per point

### Problem
`noise/web/public/tracks_yearly.json` points are 3-tuples `[lat, lon, alt_ft]`.
We lost the per-sample time, so every downstream calc fakes a sample period
(`samplePeriodS` knob in NoiseImpactTest) and then computes kt/s / fpm / drag
against that fiction. Symptoms we've hit so far:

- Takeoff acceleration values came out nonsense (hundreds of kt over what we
  thought was 20 seconds).
- Max-climb calibration is off on tracks with irregular cadence.
- Gap-suppression uses distance as a proxy for time gaps — it catches the
  worst cases but can't distinguish a slow airplane from a short data dropout.
- 20 s sliding averages drift when the real cadence differs from assumed.

### Source
`adsb.lol` trace_full rows are:

```
[sec_since_t0, lat, lon, alt_baro, ground, track, flags, vrate, details, ...]
```

The file also has `timestamp: t0` at the top level for wallclock.

### Where we dropped it
[noise/import_globe_history.py:210](import_globe_history.py#L210) —
`trace_to_points` destructures `_t, lat, lon, alt, *rest` and appends only
`[lat, lon, alt_ft]`. The `_t` never leaves the function.

### Fix

1. **Importer**
   - Change point shape to `[lat, lon, alt_ft, t_sec]` where `t_sec` is the
     row's `sec_since_t0` (integer seconds, no float precision needed).
   - Capture `t0` once per track into the track record (`t0: <epoch>`), so
     wallclock is reconstructible.
   - 4-tuple points don't break existing consumers — everyone indexes by
     position.

2. **Re-ingest**
   - Re-run `import_globe_history.py` against the cached tar files. The
     archive downloads are on disk from the overnight runs; re-extraction is
     the expensive part but we don't re-pay the download.
   - Scope: ~30,914 tracks across 109 days. Expect several hours.

3. **App side (after the re-ingest lands)**
   - Kill the `samplePeriodS` slider. Replace every use with
     `(p[3] - prev[3])`.
   - Rewrite `calibrateMaxClimb` in NoiseImpactTest to scan for a
     ≥60-real-second climb window instead of a 60-sample window.
   - Convert the 20 s sliding averages to true seconds.
   - Upgrade the gap detector to use the time delta between consecutive
     points, not distance. A real data gap is `Δt > ~30 s`.
   - The engine-state estimator's `maxGroundAccelKt` / `maxGroundDecelKt`
     percentiles become real kt/s values instead of kt-per-sample.

### Risk
Schema bump in `tracks_yearly.json`. Any consumer that still expects 3-tuples
needs a read-and-check. Grep `p\[2\]|points\[.*\]\[2\]` across `noise/web/src`
before flipping the importer.

### Status: CODE READY — waiting to launch

Importer changes applied 2026-04-15:
- `trace_to_points` now emits `[lat, lon, alt_ft, t_sec]` 4-tuples (t_sec = int).
- `extract_meta` captures `t0` (epoch seconds from the trace file's `timestamp` field).
- Track record includes `t0` for wallclock reconstruction.

**To launch the re-ingest:**
```
cd noise
python import_globe_history.py YYYY-MM-DD --scan-all
```
Run for each date in the dataset. The cached .tar files from prior overnight runs
are still on disk, so this re-extracts from cache — no re-download needed. Expect
several hours for the full 109-day sweep.

**Consumer compatibility note:** existing app code indexes points as `p[0]`, `p[1]`,
`p[2]` — a 4th element (`p[3]`) doesn't break any of those. Code that cares about
time can read `p[3]` when present, falling back to assumed sample period when it's
`undefined` (old data mixed with new).

## Other ideas for tonight (not yet scoped)

- (placeholder — add more items here as they come up)
