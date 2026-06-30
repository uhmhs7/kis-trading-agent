#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"

mkdir -p data

if [[ -f data/server.pid ]] && kill -0 "$(cat data/server.pid)" 2>/dev/null; then
  kill "$(cat data/server.pid)"
  sleep 1
fi

export KIS_ENV="${KIS_ENV:-mock}"
export KIS_ALLOW_LIVE_ORDERS="${KIS_ALLOW_LIVE_ORDERS:-false}"

setsid bash -lc "
  source '$ROOT_DIR/.venv/bin/activate'
  cd '$ROOT_DIR'
  exec uvicorn trading_agent.main:app --app-dir src --host '$HOST' --port '$PORT'
" > data/server.log 2>&1 < /dev/null &

echo $! > data/server.pid
echo "Started KIS Trading Agent on ${HOST}:${PORT} (pid $(cat data/server.pid))"

