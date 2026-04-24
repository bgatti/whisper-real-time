#!/bin/bash
# Overnight KBDU scan-all orchestrator.
# One process at a time. Waits for any running scan-all to clear, then walks
# through a sample of days across 2023-2026 using globe_history.
set -u
cd "c:/Users/Benja/OneDrive/Documents/whipser_real_time"

LOG="noise/overnight.log"
echo "=== overnight start $(date) ===" >> "$LOG"

# 1. Wait for the currently-running 2026-04-08 scan (PID 3488) to finish.
#    Polls tasklist every 30s. Times out after 30 min so we're not stuck all night.
echo "[$(date)] waiting for PID 3488 to clear (max 30 min)" >> "$LOG"
for i in $(seq 1 60); do
  if ! tasklist //FI "PID eq 3488" 2>/dev/null | grep -q 3488; then
    echo "[$(date)] PID 3488 cleared after $((i*30))s" >> "$LOG"
    break
  fi
  sleep 30
done

# 2. Process each date sequentially. Cached days will skip download.
#    Non-cached days will download ~2 GB each.
#    3 cached + 7 new ~= 10 dates.
DATES=(
  2023-04-08
  2024-04-08
  2025-04-08
  2023-07-15
  2024-07-15
  2025-07-15
  2023-10-15
  2024-10-15
  2025-10-15
  2026-01-15
)

for d in "${DATES[@]}"; do
  echo "" >> "$LOG"
  echo "=== [$(date)] scan-all $d ===" >> "$LOG"
  py -3.12 -u noise/import_globe_history.py "$d" --scan-all >> "$LOG" 2>&1 \
    || echo "[$(date)] FAILED $d (continuing)" >> "$LOG"
  sleep 60
done

echo "" >> "$LOG"
echo "=== overnight done $(date) ===" >> "$LOG"
