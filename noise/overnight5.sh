#!/bin/bash
# Batch 5: makeup for the 35 days that failed in overnight4 due to the
# PermissionError in the atomic-write path. Uses the patched import script
# (retry on replace + fallback to direct overwrite).
set -u
cd "c:/Users/Benja/OneDrive/Documents/whipser_real_time"

LOG="noise/overnight5.log"
echo "=== overnight5 start $(date) ===" >> "$LOG"

DATES=(
  # Late 2024 — overnight4 stopped here after the patch broke
  2024-11-07  2024-11-21
  2024-12-07  2024-12-21

  # All of 2025 7th/21st (all failed)
  2025-01-07  2025-01-21
  2025-02-07  2025-02-21
  2025-03-07  2025-03-21
  2025-04-07  2025-04-21
  2025-05-07  2025-05-21
  2025-06-07  2025-06-21
  2025-07-07  2025-07-21
  2025-08-07  2025-08-21
  2025-09-07  2025-09-21
  2025-10-07  2025-10-21
  2025-11-07  2025-11-21
  2025-12-07  2025-12-21

  # 2026 7th/21st (all failed)
  2026-01-07  2026-01-21
  2026-02-07  2026-02-21
  2026-03-07  2026-03-21
  2026-04-07
)

for d in "${DATES[@]}"; do
  echo "" >> "$LOG"
  echo "=== [$(date)] scan-all $d ===" >> "$LOG"
  py -3.12 -u noise/import_globe_history.py "$d" --scan-all >> "$LOG" 2>&1 \
    || echo "[$(date)] FAILED $d (continuing)" >> "$LOG"
  sleep 30
done

echo "" >> "$LOG"
echo "=== overnight5 done $(date) ===" >> "$LOG"
