#!/bin/bash
# run_reverify.sh — daily ranked re-verify pass (A.J. 2026-09-07: "run the
# reverify pass daily and report back"). Re-researches up to 50 hidden
# accounts (researched_ambiguous / staged, best trigger types first, honoring
# retry_after) and posts the run summary to Mattermost #scout-engine as Scout
# (ENGINE tier per utils/COMMS_POLICY.md — same channel as the health check).
# Fires 06:30 ET via com.teamalbert.reverify.plist, before the 07:00 health
# check so its "Retry backlog" line reflects this pass. Spend is bounded by the
# tiers + the 25/day Tavily ration; state/PAUSE and the run lock apply.

PROJECT="/Users/andrewalbertbase/Shared/AI-BOTS/TeamAlbertTriggerEventSearch"
LOG="$PROJECT/logs/reverify.log"
ALERT_ENV="${HOME}/Shared/AI-BOTS/hermes-scout-data/.env"
source "${HOME}/Shared/AI-BOTS/utils/mattermost_notify.sh" 2>/dev/null || true

mkdir -p "$PROJECT/logs"
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 3000 ]; then
    tail -2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

cd "$PROJECT" || exit 1
if [ -f "$PROJECT/state/PAUSE" ]; then
    echo "$(date '+%Y-%m-%d %H:%M') PAUSED — state/PAUSE present; skipping re-verify" >> "$LOG"
    exit 0
fi
source "$PROJECT/venv/bin/activate"

{
    echo ""
    echo "========================================"
    echo "Re-verify pass: $(date)"
    echo "========================================"
} >> "$LOG"

TMP_OUT=$(mktemp)
python enrichment_scout.py --re-enrich --reverify-unverified > "$TMP_OUT" 2>&1
EXIT_CODE=$?
grep -v -E 'NotOpenSSLWarning|warnings.warn' "$TMP_OUT" >> "$LOG"
echo "Exit code: $EXIT_CODE" >> "$LOG"

SUMMARY="$(grep -E 'Done —|another enrichment run is active|FAIL: local LLM|No .*events' "$TMP_OUT" | tail -1 | sed -E 's/^[0-9:]+ +//')"
if [ "$EXIT_CODE" -ne 0 ]; then
    MSG="$(printf '🔁 Daily re-verify pass FAILED (exit %s)\n%s\nFull log: logs/reverify.log' "$EXIT_CODE" "${SUMMARY:-no summary line}")"
else
    MSG="$(printf '🔁 Daily re-verify pass (up to 50 hidden accounts, ranked by trigger)\n%s' "${SUMMARY:-no summary line}")"
fi
mattermost_notify "$ALERT_ENV" "$MSG" scout-engine 2>>"$LOG" || true
rm -f "$TMP_OUT"
exit $EXIT_CODE
