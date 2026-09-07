#!/bin/bash
# run_enrichment.sh — triggered by launchd every 4 hours, 30 min after
# GitHub Actions scraper runs (which fires at :00 UTC / ~:00 Eastern).

PROJECT="/Users/andrewalbertbase/Shared/AI-BOTS/TeamAlbertTriggerEventSearch"
LOG="$PROJECT/logs/enrichment.log"

# Keep log to last 1000 lines so it doesn't grow forever
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 1000 ]; then
    tail -800 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

echo "" >> "$LOG"
echo "========================================" >> "$LOG"
echo "Enrichment run: $(date)" >> "$LOG"
echo "========================================" >> "$LOG"

cd "$PROJECT" || exit 1

# Pause switch (2026-09-07): 'touch state/PAUSE' skips runs without touching
# launchd — used while enrichment_scout.py is being edited/verified. Remove
# the file to resume. Logged so a forgotten pause is visible in the log.
if [ -f "$PROJECT/state/PAUSE" ]; then
    echo "PAUSED — state/PAUSE present since $(stat -f %Sm "$PROJECT/state/PAUSE"); skipping run" >> "$LOG"
    exit 0
fi
source "$PROJECT/venv/bin/activate"
python enrichment_scout.py >> "$LOG" 2>&1

echo "Exit code: $?" >> "$LOG"
