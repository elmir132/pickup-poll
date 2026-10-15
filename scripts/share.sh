#!/usr/bin/env bash
# Share the real app with the club from this Mac, through a free Cloudflare quick tunnel.
#   ./scripts/share.sh
# It prints a link for the players and the organizer PIN. Stop it with Ctrl+C.
# The link changes every time you start it, and it only works while this Mac is on, awake and online.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "cloudflared is not installed. Install it with: brew install cloudflared" >&2
  exit 1
fi

PORT="${PORT:-8787}"
PIN="${PICKUP_POLL_PIN:-$(python3 -c 'import secrets; print("".join(secrets.choice("0123456789") for _ in range(6)))')}"
LOG="$(mktemp)"
cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" >"$LOG" 2>&1 &
TUNNEL=$!
trap 'kill "$TUNNEL" 2>/dev/null || true; rm -f "$LOG"' EXIT INT TERM

URL=""
for _ in $(seq 1 80); do
  URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" | head -1 || true)"
  [ -n "$URL" ] && break
  sleep 0.5
done
if [ -z "$URL" ]; then
  echo "The tunnel did not start. Its log:" >&2
  cat "$LOG" >&2
  exit 1
fi

echo
echo "  Share this link with the club:   $URL"
echo "  Organizer PIN (keep it private): $PIN"
echo
python3 app.py --host 127.0.0.1 --port "$PORT" --organizer-pin "$PIN" --allow-host "${URL#https://}" --trust-proxy
