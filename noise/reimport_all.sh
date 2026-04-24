#!/usr/bin/env bash
# Re-import all 109 dates to pick up the [lat,lon,alt,t_sec] schema change.
# Uses cached tar files — no re-download. Each date re-extracts, re-parses,
# and merges into tracks_yearly.json with the new 4-tuple points + t0 field.
#
# Usage: bash noise/reimport_all.sh 2>&1 | tee noise/reimport.log

set -e
cd "$(dirname "$0")"
PYTHON="/c/Users/Benja/AppData/Local/Programs/Python/Python312/python.exe"

# Ordered: most recent first, round-robin across years so each pass adds
# one date from 2026, 2025, 2024, 2023. Gets recent data fast + even
# year coverage early.
DATES=(
2026-04-10 2025-12-21 2024-12-21 2023-12-21 2026-04-09 2025-12-07 2024-12-07
2023-12-07 2026-04-08 2025-11-21 2024-11-21 2023-11-21 2026-04-07 2025-11-15
2024-11-15 2023-11-15 2026-03-21 2025-11-07 2024-11-07 2023-11-07 2026-03-15
2025-10-21 2024-10-21 2023-10-21 2026-03-07 2025-10-15 2024-10-15 2023-10-15
2026-02-21 2025-10-07 2024-10-07 2023-10-07 2026-02-15 2025-09-21 2024-09-21
2023-09-21 2026-02-07 2025-09-15 2024-09-15 2023-09-15 2026-01-21 2025-09-07
2024-09-07 2023-09-07 2026-01-15 2025-08-21 2024-08-21 2023-08-21 2026-01-12
2025-08-15 2024-08-15 2023-08-15 2026-01-07 2025-08-07 2024-08-07 2023-08-07
2026-01-01 2025-07-21 2024-07-21 2023-07-21 2025-07-15 2024-07-15 2023-07-15
2025-07-07 2024-07-07 2023-07-07 2025-06-21 2024-06-21 2023-06-21 2025-06-15
2024-06-15 2023-06-15 2025-05-21 2024-06-07 2023-06-07 2025-05-15 2024-05-21
2023-05-21 2025-05-07 2024-05-15 2023-05-15 2025-04-21 2024-05-07 2023-05-07
2025-04-08 2024-04-21 2023-04-21 2025-04-07 2024-04-08 2023-04-08 2025-03-21
2024-04-07 2023-04-07 2025-03-15 2024-03-21 2023-03-15 2025-03-07 2024-03-15
2025-02-21 2024-03-07 2025-02-07 2024-02-21 2025-01-21 2024-02-07 2025-01-15
2024-01-21 2025-01-07 2024-01-15 2024-01-07
)

TOTAL=${#DATES[@]}
echo "=== reimport_all: $TOTAL dates ==="
echo "=== started: $(date) ==="
echo ""

DONE=0
FAIL=0
for d in "${DATES[@]}"; do
  DONE=$((DONE + 1))
  echo "[$DONE/$TOTAL] $d ..."
  if "$PYTHON" import_globe_history.py "$d" --scan-all; then
    echo "  ✓ $d done"
  else
    echo "  ✗ $d FAILED (exit $?)"
    FAIL=$((FAIL + 1))
  fi
  echo ""
done

echo "=== finished: $(date) ==="
echo "=== $DONE dates processed, $FAIL failures ==="
