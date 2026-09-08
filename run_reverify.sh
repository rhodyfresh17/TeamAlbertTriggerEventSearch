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
# Phase 4 (2026-09-08): nightly trigger expiry runs FIRST so the re-verify
# pass never spends a search on a trigger that is already past its shelf
# life (CFO/seat/funding 60d, M&A 120d, expansion 90d, stable_target 365d).
# Expired events are tombstoned 'trigger_expired' (research kept) and the
# account's best trigger / grade are recomputed from what remains. The
# script takes the enrichment run lock itself and exits 0 when a run is
# active (review 2026-09-08 (Phase 4), 5d).
EXPIRE_RC=0
EXPIRE_LINE=""
if [ -f "$PROJECT/scripts/expire_triggers.py" ]; then
    python scripts/expire_triggers.py --apply > "$TMP_OUT" 2>&1
    EXPIRE_RC=$?
    grep -v -E 'NotOpenSSLWarning|warnings.warn' "$TMP_OUT" >> "$LOG"
    echo "Expiry exit code: $EXPIRE_RC" >> "$LOG"
    EXPIRE_LINE="$(grep -iE 'expired|nothing to do|would tombstone|tombstoned|expiry skipped' "$TMP_OUT" | tail -1 | sed -E 's/^[0-9:]+ +//' | cut -c1-200)"
fi
python enrichment_scout.py --re-enrich --reverify-unverified > "$TMP_OUT" 2>&1
EXIT_CODE=$?
grep -v -E 'NotOpenSSLWarning|warnings.warn' "$TMP_OUT" >> "$LOG"
echo "Exit code: $EXIT_CODE" >> "$LOG"

SUMMARY="$(grep -E 'Done —|another enrichment run is active|FAIL: local LLM|No .*events' "$TMP_OUT" | tail -1 | sed -E 's/^[0-9:]+ +//')"
if [ "$EXIT_CODE" -ne 0 ]; then
    MSG="$(printf '🔁 Daily re-verify pass FAILED (exit %s)\n%s\nFull log: logs/reverify.log' "$EXIT_CODE" "${SUMMARY:-no summary line}")"
else
    MSG="$(printf '🔁 Daily re-verify pass (up to 50 hidden accounts, ranked by trigger)\n%s%s' "${SUMMARY:-no summary line}" "${EXPIRE_LINE:+
⏳ Expiry: $EXPIRE_LINE}")"
fi
# Review 2026-09-08 (Phase 4), 5f: EXPIRE_RC was captured and then ignored —
# a failed expiry (a traceback, a Supabase error) posted a normal-looking
# summary and the wrapper exited 0, so a broken nightly expiry was
# invisible. Lead the message with it and fail the wrapper.
if [ "$EXPIRE_RC" -ne 0 ]; then
    MSG="$(printf '⚠️ Expiry FAILED (exit %s) — see logs/reverify.log\n%s' "$EXPIRE_RC" "$MSG")"
fi
mattermost_notify "$ALERT_ENV" "$MSG" scout-engine 2>>"$LOG" || true
rm -f "$TMP_OUT"
# Non-zero when EITHER step failed (the re-verify exit wins when both did).
FINAL_RC=$EXIT_CODE
if [ "$FINAL_RC" -eq 0 ] && [ "$EXPIRE_RC" -ne 0 ]; then
    FINAL_RC=$EXPIRE_RC
fi
exit $FINAL_RC
