#!/bin/bash
# run_health_check.sh — launchd wrapper for monitor_health.py
#
# Fires daily at 7am Eastern (per com.teamalbert.healthcheck.plist).
# Runs the health check in the Mac's native venv (where deps + Ollama work)
# and writes structured output to logs/health_alerts.log for Elon to read.

PROJECT="/Users/andrewalbertbase/Shared/AI-BOTS/TeamAlbertTriggerEventSearch"
ALERTS_LOG="$PROJECT/logs/health_alerts.log"
RUNTIME_LOG="$PROJECT/logs/health_check_runtime.log"

# Trim alerts log if it gets too big (keep last 5000 lines so Elon has history)
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
else
    # Also log if there are WARNs even though overall passed
    if grep -qE "^  🟡 WARN  " "$TMP_OUT"; then
        {
            echo ""
            echo "[$TIMESTAMP] 🟡 Health check passed with WARNINGS (mode=$MODE)"
            grep -E "^  🟡 WARN  " "$TMP_OUT" | sed 's/^/    /'
        } >> "$ALERTS_LOG"
    else
        echo "[$TIMESTAMP] ✅ All clear ($MODE check passed)" >> "$ALERTS_LOG"
    fi
fi

rm -f "$TMP_OUT"
exit $EXIT_CODE
