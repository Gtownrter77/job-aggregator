#!/usr/bin/env bash
# Start the cron-free scheduler if it is not already running.
APP=/workspace/job-aggregator
cd "$APP" || exit 0
mkdir -p logs
pid="$(cat logs/scheduler.pid 2>/dev/null || true)"
if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && grep -qa 'scheduler-bg' "/proc/$pid/cmdline" 2>/dev/null; then
  echo "scheduler already running (pid $pid)"
  exit 0
fi
rm -f logs/scheduler.pid
setsid -f ./scripts/scheduler-bg.sh </dev/null >> logs/scheduler.log 2>&1
sleep 1
pid="$(cat logs/scheduler.pid 2>/dev/null || true)"
if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
  echo "started scheduler pid $pid"
  exit 0
fi
echo "ERROR: scheduler failed to start (see logs/scheduler.log)" >&2
exit 1
