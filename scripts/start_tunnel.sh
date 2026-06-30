#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -x tools/cloudflared ]]; then
  echo "tools/cloudflared is missing. Download it first:"
  echo "curl -L --fail -o tools/cloudflared https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
  exit 1
fi

mkdir -p data

if [[ -f data/tunnel.pid ]] && kill -0 "$(cat data/tunnel.pid)" 2>/dev/null; then
  kill "$(cat data/tunnel.pid)"
  sleep 1
fi

setsid bash -lc "
  cd '$ROOT_DIR'
  exec ./tools/cloudflared tunnel --url http://127.0.0.1:8000 --no-autoupdate
" > data/tunnel.log 2>&1 < /dev/null &

echo $! > data/tunnel.pid
sleep 8

URL="$(grep -Eo 'https://[-a-z0-9]+\\.trycloudflare\\.com' data/tunnel.log | tail -1 || true)"
if [[ -n "$URL" ]]; then
  echo "$URL" > data/public_url.txt
  echo "Tunnel URL: $URL"
else
  echo "Tunnel started, but URL was not found yet. Check data/tunnel.log"
fi

