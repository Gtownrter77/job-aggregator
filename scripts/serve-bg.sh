#!/usr/bin/env bash
# Start the web UI in the background (port from config.yaml, default 8765).
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
if [ -f logs/server.pid ] && kill -0 "$(cat logs/server.pid)" 2>/dev/null; then
  echo "already running (pid $(cat logs/server.pid))"; exit 0
fi
nohup ./.venv/bin/python -m aggregator serve "$@" >> logs/server.log 2>&1 &
echo $! > logs/server.pid
echo "started pid $(cat logs/server.pid); logs/server.log"
