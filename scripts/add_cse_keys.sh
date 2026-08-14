#!/bin/bash
# Securely add Google CSE credentials to .env — prompts hide your typing,
# nothing is echoed to the terminal, chat, or shell history.
set -euo pipefail
cd "$(dirname "$0")/.."

if grep -q '^GOOGLE_CSE_KEY=' .env 2>/dev/null; then
  echo "GOOGLE_CSE_KEY already exists in .env — remove the old lines first if you're rotating."
  exit 1
fi

echo "Paste the API key (starts with 'AIza'), then press Enter (typing is hidden):"
read -rs CSE_KEY
echo "Paste the Search engine ID (cx), then press Enter (typing is hidden):"
read -rs CSE_CX

if [[ ! "$CSE_KEY" =~ ^AIza ]]; then
  echo "FAIL — that API key doesn't start with 'AIza'. Nothing was saved; run me again."
  exit 1
fi
if [[ -z "$CSE_CX" ]]; then
  echo "FAIL — empty Search engine ID. Nothing was saved; run me again."
  exit 1
fi
if [[ "$CSE_CX" =~ ^AIza ]]; then
  echo "FAIL — that's the API key again, not the Search engine ID."
  echo "The Search engine ID is on programmablesearchengine.google.com →"
  echo "your engine → Overview, labeled 'Search engine ID' (it does NOT"
  echo "start with AIza). Nothing was saved; run me again."
  exit 1
fi

{
  echo ""
  echo "# Google Custom Search JSON API — free 100 queries/day, daily reset."
  echo "# Fallback search rung; activates automatically when these are set."
  echo "GOOGLE_CSE_KEY=$CSE_KEY"
  echo "GOOGLE_CSE_CX=$CSE_CX"
} >> .env

echo "PASS — both values saved to .env (key length: ${#CSE_KEY}, cx length: ${#CSE_CX})."
echo "Now tell Claude to verify."
