#!/bin/bash
# Batch 4: 7th + 21st of every month across 2023-2026 (2023 skips Jan,
# 2026 stops at 04-07 since 04-21 is the future).
set -u
cd "c:/Users/Benja/OneDrive/Documents/whipser_real_time"

LOG="noise/overnight4.log"
echo "=== overnight4 start $(date) ===" >> "$LOG"

# Wait for any in-flight import to clear
clean_hits=0
while [ $clean_hits -lt 2 ]; do
  if wmic process where "name='python.exe'" get CommandLine 2>/dev/null | grep -q "import_globe_history"; then
    clean_hits=0
    sleep 30
    continue
  fi
  clean_hits=$((clean_hits + 1))
  sleep 15
done

echo "[$(date)] clear — starting 77-day batch" >> "$LOG"

DATES=(
  # 2023 — skip January (archive didn't exist)
  2023-02-07  2023-02-21
  2023-03-07  2023-03-21
  2023-04-07  2023-04-21
  2023-05-07  2023-05-21
  2023-06-07  2023-06-21
  2023-07-07  2023-07-21
  2023-08-07  2023-08-21
  2023-09-07  2023-09-21
  2023-10-07  2023-10-21
  2023-11-07  2023-11-21
  2023-12-07  2023-12-21

  # 2024 — full year
  2024-01-07  2024-01-21
  2024-02-07  2024-02-21
  2024-03-07  2024-03-21
  2024-04-07  2024-04-21
  2024-05-07  2024-05-21
  2024-06-07  2024-06-21
  2024-07-07  2024-07-21
  2024-08-07  2024-08-21
  2024-09-07  2024-09-21
  2024-10-07  2024-10-21
  2024-11-07  2024-11-21
  2024-12-07  2024-12-21

  # 2025 — full year
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

  # 2026 — year to date (cap at 04-07; 04-21 is future)
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
echo "=== overnight4 done $(date) ===" >> "$LOG"
