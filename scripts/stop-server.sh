#!/usr/bin/env bash
cd "$(dirname "$0")/.."
[ -f logs/server.pid ] && kill "$(cat logs/server.pid)" 2>/dev/null && rm -f logs/server.pid && echo stopped || echo "not running"
