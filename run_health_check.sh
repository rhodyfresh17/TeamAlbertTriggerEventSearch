#!/bin/bash
# run_health_check.sh — launchd wrapper for monitor_health.py
#
# Fires daily at 7am Eastern (per com.teamalbert.healthcheck.plist).
# Runs the health check in the Mac's native venv (where deps + Ollama work)
# and writes structured output to logs/health_alerts.log, which Scout's Monday
# cron reads to produce the plain-English weekly summary.

PROJECT="/Users/andrewalbertbase/Shared/AI-BOTS/TeamAlbertTriggerEventSearch"
ALERTS_LOG="$PROJECT/logs/health_alerts.log"
RUNTIME_LOG="$PROJECT/logs/health_check_runtime.log"

# ENGINE tier (utils/COMMS_POLICY.md) -> Mattermost #scout-engine, posted as SCOUT.
# Migrated off Discord 2026-08-10. Moved Elon -> Scout 2026-08-15.
#
# WHY SCOUT (A.J.'s call, 2026-08-15): TriggerEventSearch is a sales engine, and
# ownership now sits with Scout end to end — the repo is mounted in his container
# and his Monday cron "Lead Sourcing Engine Status Check" reads the very log this
# script writes and posts the plain-English summary. He judges lead quality against
# the ICP, which lives in his vault. Raw signal and interpretation therefore live in
# the SAME channel, which is the whole point of not splitting them.
#
# ALERT_ENV MUST match the target channel's owner — it supplies the bot TOKEN, and
# on a resolution failure mattermost_notify falls back to THAT file's
# MATTERMOST_ENGINE_CHANNEL. #scout-engine is a PRIVATE channel, so Elon's token
# 404s on it: leaving this pointed at hermes-elon-data would have sent every alert
# right back to #elon-engine, silently. Verified 2026-08-15.
#
# Only fires on WARN/FAIL below, plus a Monday all-clear so a dead check can't
# look like a healthy one.
ALERT_ENV="${HOME}/Shared/AI-BOTS/hermes-scout-data/.env"
source "${HOME}/Shared/AI-BOTS/utils/mattermost_notify.sh" 2>/dev/null || true

# Trim alerts log if it gets too big (keep last 5000 lines so Scout has history)
if [ -f "$ALERTS_LOG" ] && [ "$(wc -l < "$ALERTS_LOG")" -gt 5000 ]; then
    tail -4000 "$ALERTS_LOG" > "$ALERTS_LOG.tmp" && mv "$ALERTS_LOG.tmp" "$ALERTS_LOG"
fi

# Trim runtime log similarly
if [ -f "$RUNTIME_LOG" ] && [ "$(wc -l < "$RUNTIME_LOG")" -gt 1000 ]; then
    tail -800 "$RUNTIME_LOG" > "$RUNTIME_LOG.tmp" && mv "$RUNTIME_LOG.tmp" "$RUNTIME_LOG"
fi

cd "$PROJECT" || exit 1
source "$PROJECT/venv/bin/activate"

# Pick mode based on day of week — Monday = weekly (deep), other days = daily
DAY=$(date +%u)   # 1=Mon ... 7=Sun
if [ "$DAY" = "1" ]; then
    MODE="--weekly"
else
    MODE="--daily"
fi

# Capture stdout to a tmp file so we can both log it AND parse it
TMP_OUT=$(mktemp)
python3 monitor_health.py $MODE > "$TMP_OUT" 2>>"$RUNTIME_LOG"
EXIT_CODE=$?

# Always echo to runtime log for debugging
{
    echo "========================================"
    echo "Health check run: $(date -u +'%Y-%m-%d %H:%M UTC') mode=$MODE exit=$EXIT_CODE"
    echo "========================================"
    cat "$TMP_OUT"
} >> "$RUNTIME_LOG"

# Append to alerts log — only on FAIL, otherwise a single "All clear" line
TIMESTAMP=$(date -u +'%Y-%m-%d %H:%M UTC')
# Filter — only include lines that are individual check rows (start with
# two spaces + emoji + STATUS + double-space + check name). Skips the
# summary lines like "Overall: 🟡 WARN" which would pollute the log.
EXTRACT_PATTERN='^  (🔴|🟡) (FAIL|WARN)  '

if [ "$EXIT_CODE" -ne 0 ]; then
    {
        echo ""
        echo "[$TIMESTAMP] 🔴 HEALTH CHECK FAILED (mode=$MODE, exit=$EXIT_CODE)"
        grep -E "$EXTRACT_PATTERN" "$TMP_OUT" | sed 's/^/    /'
        echo "    → Full output in logs/health_check_runtime.log"
    } >> "$ALERTS_LOG"
    DETAIL="$(grep -E "$EXTRACT_PATTERN" "$TMP_OUT" | sed 's/^/• /')"
    mattermost_notify "$ALERT_ENV" "$(printf '🔴 TriggerEventSearch health FAILED (%s)\n%s\nFull log: logs/health_check_runtime.log' "$MODE" "$DETAIL")" scout-engine
else
    # Also log if there are WARNs even though overall passed
    if grep -qE "^  🟡 WARN  " "$TMP_OUT"; then
        {
            echo ""
            echo "[$TIMESTAMP] 🟡 Health check passed with WARNINGS (mode=$MODE)"
            grep -E "^  🟡 WARN  " "$TMP_OUT" | sed 's/^/    /'
        } >> "$ALERTS_LOG"
        DETAIL="$(grep -E "^  🟡 WARN  " "$TMP_OUT" | sed 's/^/• /')"
        mattermost_notify "$ALERT_ENV" "$(printf '🟡 TriggerEventSearch passed with warnings (%s)\n%s' "$MODE" "$DETAIL")" scout-engine
    else
        echo "[$TIMESTAMP] ✅ All clear ($MODE check passed)" >> "$ALERTS_LOG"
        # Weekly heartbeat (Mondays only) — posts a positive "still alive" line to
        # the #scout-engine channel. Without it, a health check that has itself
        # died looks identical to a healthy one: silence either way. Matches the
        # Monday cadence of Scout's "Lead Sourcing Engine Status Check" cron.
        if [ "$(date +%u)" -eq 1 ]; then
            WEEKLY_SUMMARY="$(grep -E '^(Summary|Overall):' "$TMP_OUT" | paste -sd' · ' -)"
            mattermost_notify "$ALERT_ENV" "$(printf '🟢 TriggerEventSearch weekly all-clear (%s)\n%s' "$MODE" "${WEEKLY_SUMMARY:-check passed, no warnings}")" scout-engine
        fi
    fi
fi

rm -f "$TMP_OUT"

exit $EXIT_CODE
