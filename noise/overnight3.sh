#!/bin/bash
# Batch 3: fill out 2026 to ~10 evenly-spread days. Waits for any running
# import_globe_history.py to finish first (polls tasklist).
set -u
cd "c:/Users/Benja/OneDrive/Documents/whipser_real_time"

LOG="noise/overnight3.log"
echo "=== overnight3 start $(date) ===" >> "$LOG"

# Poll for any running python import job. Exit the wait loop when none
# remain for 60 consecutive seconds (two clean checks).
clean_hits=0
while [ $clean_hits -lt 2 ]; do
  if tasklist 2>/dev/null | grep -qi "python"; then
    # Check if any python has import_globe_history in its command line
    if wmic process where "name='python.exe'" get CommandLine 2>/dev/null | grep -q "import_globe_history"; then
      clean_hits=0
      echo "[$(date)] waiting — import job still running" >> "$LOG"
      sleep 30
      continue
    fi
  fi
  clean_hits=$((clean_hits + 1))
  sleep 30
done

echo "[$(date)] clear — starting 2026 fill" >> "$LOG"

DATES=(
  2026-01-01
  2026-01-12
  2026-01-23
  2026-02-03
  2026-02-25
  2026-03-08
  2026-03-19
  2026-03-30
)

for d in "${DATES[@]}"; do
  echo "" >> "$LOG"
  echo "=== [$(date)] scan-all $d ===" >> "$LOG"
  py -3.12 -u noise/import_globe_history.py "$d" --scan-all >> "$LOG" 2>&1 \
    || echo "[$(date)] FAILED $d (continuing)" >> "$LOG"
  sleep 30
done

echo "" >> "$LOG"
echo "=== overnight3 done $(date) ===" >> "$LOG"
