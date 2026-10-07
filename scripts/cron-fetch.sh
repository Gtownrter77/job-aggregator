#!/usr/bin/env bash
# Cron wrapper: run a fetch with the project's venv and append to logs/fetch.log.
# Install with:  crontab -e   then add e.g.
#   0 */6 * * * /path/to/job-aggregator/scripts/cron-fetch.sh
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
# flock prevents overlapping runs if one takes longer than the interval
exec /usr/bin/flock -n /tmp/job-aggregator-fetch.lock \
  ./.venv/bin/python -m aggregator fetch >> logs/fetch.log 2>&1
