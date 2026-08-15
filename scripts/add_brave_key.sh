#!/bin/bash
# Securely add the Brave Search API key to .env — hidden prompt, nothing
# echoed to terminal, chat, or shell history.
set -euo pipefail
cd "$(dirname "$0")/.."

if grep -q '^BRAVE_SEARCH_API_KEY=' .env 2>/dev/null; then
  echo "BRAVE_SEARCH_API_KEY already exists in .env — remove the old line first if rotating."
  exit 1
fi

echo "Paste the Brave Search API key, then press Enter (typing is hidden):"
read -rs BKEY
if [[ -z "$BKEY" || ${#BKEY} -lt 20 ]]; then
  echo "FAIL — that doesn't look like a Brave API key. Nothing was saved; run me again."
  exit 1
fi

{
  echo ""
  echo "# Brave Search API — free 2,000 queries/month. Fallback search rung;"
  echo "# activates automatically when set."
  echo "BRAVE_SEARCH_API_KEY=$BKEY"
} >> .env

echo "PASS — saved to .env (key length: ${#BKEY})."
echo "Now tell Claude to verify."
