#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f data/tunnel.pid ]] && kill -0 "$(cat data/tunnel.pid)" 2>/dev/null; then
  kill "$(cat data/tunnel.pid)"
  echo "Stopped tunnel pid $(cat data/tunnel.pid)"
else
  echo "No running tunnel pid found."
fi

