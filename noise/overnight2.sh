#!/bin/bash
# Batch 2: fill each year to ~10 days by grabbing the 15th of each missing
# month. Runs one date at a time. Failures are logged and skipped so one
# bad day can't abort the whole run.
set -u
cd "c:/Users/Benja/OneDrive/Documents/whipser_real_time"

LOG="noise/overnight2.log"
echo "=== overnight2 start $(date) ===" >> "$LOG"

DATES=(
  # 2023 — fill months not already covered (04, 07, 10 are done)
  2023-01-15
  2023-03-15
  2023-05-15
  2023-06-15
  2023-08-15
  2023-09-15
  2023-11-15

  # 2024 — same pattern
  2024-01-15
  2024-03-15
  2024-05-15
  2024-06-15
  2024-08-15
  2024-09-15
  2024-11-15

  # 2025 — same pattern
  2025-01-15
  2025-03-15
  2025-05-15
  2025-06-15
  2025-08-15
  2025-09-15
  2025-11-15

  # 2026 — year to date (already have 01-15, 04-08)
  2026-02-15
  2026-03-15
  2026-04-09
  2026-04-10
  2026-04-11
)

for d in "${DATES[@]}"; do
  echo "" >> "$LOG"
  echo "=== [$(date)] scan-all $d ===" >> "$LOG"
  py -3.12 -u noise/import_globe_history.py "$d" --scan-all >> "$LOG" 2>&1 \
    || echo "[$(date)] FAILED $d (continuing)" >> "$LOG"
  sleep 30
done

echo "" >> "$LOG"
echo "=== overnight2 done $(date) ===" >> "$LOG"
