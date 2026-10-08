#!/usr/bin/env bash
# Unattended job check: ensure Ollama + UI are up, then run
#   python -m aggregator auto
# (fetch both tracks, rescore, auto-qualify up to 5 strong NEW matches with
# Ollama-written follow-up DRAFTS, write logs/digest-*.md). Never sends email.
# With dashboard.publish: true (config.local.yaml) `auto` also refreshes the phone
# dashboard on GitHub Pages (gh-pages branch only; fails soft).
#
# Cron (America/New_York, box clock is already ET), weekdays only:
#   19 7,11,16 * * 1-5  /workspace/job-aggregator/scripts/auto_run.sh
set -uo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
export PATH="/usr/local/bin:$PATH"
# flock so two cron ticks never overlap (fetch can take ~7 min); -o so the
# background servers started below do not inherit (and keep holding) the lock
/usr/bin/flock -n -o -E 75 logs/.auto_run.lock -c '
  set -uo pipefail
  echo "===== $(date "+%Y-%m-%d %H:%M:%S %Z") auto_run start =====" >> logs/auto_run.log
  ./scripts/ollama-bg.sh >> logs/auto_run.log 2>&1 || true
  ./scripts/serve-bg.sh  >> logs/auto_run.log 2>&1 || true
  # pid alive but not answering -> restart the UI
  if ! curl -fsS -m 10 -o /dev/null http://127.0.0.1:8765/ 2>/dev/null; then
    sleep 5
    if ! curl -fsS -m 10 -o /dev/null http://127.0.0.1:8765/ 2>/dev/null; then
      echo "UI not answering; restarting" >> logs/auto_run.log
      ./scripts/stop-server.sh >> logs/auto_run.log 2>&1; sleep 2
      ./scripts/serve-bg.sh >> logs/auto_run.log 2>&1 || true
    fi
  fi
  ./.venv/bin/python -m aggregator auto >> logs/auto_run.log 2>&1
  rc=$?
  echo "===== $(date "+%Y-%m-%d %H:%M:%S %Z") auto_run end (rc=$rc) =====" >> logs/auto_run.log
  exit $rc
'
rc=$?
if [ "$rc" = 75 ]; then
  echo "$(date "+%Y-%m-%d %H:%M:%S %Z") auto_run skipped: previous run still in progress (lock held)" >> logs/auto_run.log
  exit 0
fi
exit $rc
