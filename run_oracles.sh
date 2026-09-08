#!/bin/bash
# run_oracles.sh — monthly (2nd of the month, 05:00 ET via com.teamalbert.oracles.plist):
#   1. refresh the free oracle tables in state/oracles.db (SEC IAPD adviser
#      feed published on the 1st; FDIC active-bank list) — zero-search
#      verification for RIAs / banks in enrichment Stage A;
#   2. emit "New SEC-registered investment adviser" trigger events for
#      in-territory, in-band registrations of the last 75 days;
#   3. post a short summary to Mattermost #scout-engine (engine tier).
# Research 2026-09-08: ~1,000 new SEC registrations/yr in territory, ~50 in-band.
# Honors state/PAUSE. Safe to re-run: the trigger keeps a per-firm ledger.

PROJECT="/Users/andrewalbertbase/Shared/AI-BOTS/TeamAlbertTriggerEventSearch"
LOG="$PROJECT/logs/oracles.log"
ALERT_ENV="${HOME}/Shared/AI-BOTS/hermes-scout-data/.env"
source "${HOME}/Shared/AI-BOTS/utils/mattermost_notify.sh" 2>/dev/null || true

# Summary lines for the Mattermost post (review 2026-09-08). The old
# extraction — `grep -iE 'rows|firms|banks|emitted|would emit|nothing|error|fail'
# | tail -4` — kept the WRONG four lines: refresh_oracles.py prints one
# oracle_meta line per source AFTER the count lines and each one matches
# 'rows', so the in-territory counts, ria_trigger.py's
# 'Selection: scanned=… to_emit=N' and, on a failure, 'ria: FAILED — <exception>'
# were pushed out of the tail. This matches the REAL print lines of
# scripts/refresh_oracles.py and scripts/ria_trigger.py:
#   'HH:MM:SS  ria_firm (SEC IAPD advisers): 23,812 rows (in-territory 4,102, …)'
#   'HH:MM:SS  bank (FDIC BankFind, index …): 4,512 rows (in-territory 812, …)'
#   'HH:MM:SS  ria: FAILED — HTTPError: … (previous table kept)'
#   'Selection: scanned=23812 · outside_window=… · to_emit=3'
#   'APPLIED — emitted 3 event(s) to Supabase (…)'
#   'RIA trigger: nothing to do — 0 new in-band registrations to emit. Exit 0.'
#   'DRY RUN — would emit 3 event(s) …'  /  'ERROR: upsert failed after 0 row(s): …'
# plus the last line of an uncaught traceback ('<Something>Error: …'), and
# drops oracle_meta. '[0-9] rows' (not bare 'rows') so a firm-table row for
# "Narrows Capital" never sneaks in. The timestamp prefix is stripped.
# A function so tests/test_monitor_health.py can run real sample output
# through it. At most 6 lines: a full run yields 4-6 matches, failures first.
oracle_summary() {
    grep -v -E 'NotOpenSSLWarning|warnings.warn' \
        | grep -E 'FAILED|ERROR|[A-Za-z]+Error:|APPLIED|nothing to do|would emit [0-9]|Selection:|[0-9] rows' \
        | grep -v 'oracle_meta' \
        | sed -E 's/^[0-9]{2}:[0-9]{2}:[0-9]{2}  //' \
        | head -6 | cut -c1-300
}

mkdir -p "$PROJECT/logs"
# Keep the log bounded like the other wrappers (review 2026-09-08): each run
# appends the full firm table, and nothing trimmed it.
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 3000 ]; then
    tail -2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

cd "$PROJECT" || exit 1
if [ -f "$PROJECT/state/PAUSE" ]; then
    echo "$(date '+%Y-%m-%d %H:%M') PAUSED — state/PAUSE present; skipping oracle refresh" >> "$LOG"
    exit 0
fi
source "$PROJECT/venv/bin/activate"
{
    echo ""; echo "========================================"
    echo "Oracle refresh: $(date)"; echo "========================================"
} >> "$LOG"

TMP_OUT=$(mktemp)
python scripts/refresh_oracles.py --source all > "$TMP_OUT" 2>&1
RC1=$?
python scripts/ria_trigger.py --apply >> "$TMP_OUT" 2>&1
RC2=$?
grep -v -E 'NotOpenSSLWarning|warnings.warn' "$TMP_OUT" >> "$LOG"
echo "Exit codes: refresh=$RC1 trigger=$RC2" >> "$LOG"

SUMMARY="$(oracle_summary < "$TMP_OUT")"
if [ "$RC1" -ne 0 ] || [ "$RC2" -ne 0 ]; then
    # The trigger only sees registrations from the last 75 days, so a failed
    # month left alone loses that month's new advisers for good (review
    # 2026-09-08); the per-firm ledger makes a re-run idempotent.
    MSG="$(printf '🗄️ Monthly oracle refresh FAILED (refresh=%s trigger=%s)\n%s\n→ re-run run_oracles.sh before next month — the ledger makes it idempotent (the 75-day trigger window does not wait). Full log: logs/oracles.log' "$RC1" "$RC2" "${SUMMARY:-no output}")"
else
    MSG="$(printf '🗄️ Monthly oracle refresh + new-adviser triggers\n%s' "${SUMMARY:-done}")"
fi
mattermost_notify "$ALERT_ENV" "$MSG" scout-engine 2>>"$LOG" || true
rm -f "$TMP_OUT"
[ "$RC1" -eq 0 ] && [ "$RC2" -eq 0 ]
