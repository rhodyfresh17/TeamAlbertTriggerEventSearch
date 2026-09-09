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
RC=$?

echo "Exit code: $RC" >> "$LOG"

# Report a failed run. Until 2026-09-08 the last line of this script was
# `echo "Exit code: $?"` — the echo SUCCEEDS, so the script always exited 0 no
# matter what enrichment did. Six runs a day, no alert, and launchd saw success
# every time. The daily health check would catch the CONSEQUENCE (stale events)
# the next morning, but the job itself said nothing.
if [ "$RC" -ne 0 ]; then
    ALERT_ENV="${HOME}/Shared/AI-BOTS/hermes-scout-data/.env"
    source "${HOME}/Shared/AI-BOTS/utils/mattermost_notify.sh" 2>/dev/null || true
    TAIL="$(tail -5 "$LOG" 2>/dev/null)"
    mattermost_notify "$ALERT_ENV" \
        "$(printf '\xf0\x9f\x94\xb4 Lead-sourcing enrichment FAILED (exit %s)\n%s\nFull log: logs/enrichment.log' "$RC" "$TAIL")" \
        scout-engine 2>>"$LOG" || true
fi

exit $RC
