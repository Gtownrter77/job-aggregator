#!/usr/bin/env bash
# Cron-free weekday scheduler for job-aggregator.
# Runs as a long-lived daemon: every minute it checks whether a 07:19 / 11:19 / 16:19
# America/New_York weekday slot is due, and if so launches scripts/auto_run.sh once.
# Also re-runs scripts/boot.sh (health) every 10 minutes.
# Safe without the system `cron` package (Debian mirror 500s on this box).
# Never sends email. Logs: logs/scheduler.log
set -uo pipefail

APP=/workspace/job-aggregator
cd "$APP" || exit 0
mkdir -p logs

export HOME=/home/box USER=box LOGNAME=box
export PATH=/usr/local/bin:/usr/bin:/bin
export JOBAGG_SCHEDULER=1
umask 022

LOG="$APP/logs/scheduler.log"
PIDF="$APP/logs/scheduler.pid"
SLOTS="07:19 11:19 16:19"

# single instance
exec 8>"$APP/logs/.scheduler.lock"
flock -n 8 || exit 0
echo $$ > "$PIDF"

log() { echo "$(date '+%F %T %Z') $*" >> "$LOG"; }
# keep log bounded
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 4000 ]; then
  tail -n 2000 "$LOG" > "$LOG.tmp" && mv -f "$LOG.tmp" "$LOG"
fi

log "scheduler start pid=$$"
trap 'log "scheduler stop"; rm -f "$PIDF"; exit 0' TERM INT

last_health=0
while true; do
  now="$(date +%s)"
  day="$(date +%F)"; dow="$(date +%u)"   # 1=Mon .. 7=Sun; box clock is America/New_York
  hm="$(date +%H:%M)"

  # health every 10 min (boot.sh is idempotent + flocked)
  if [ $((now - last_health)) -ge 600 ]; then
    last_health=$now
    JOBAGG_BOOT_ACTIVE=1 "$APP/scripts/boot.sh" --from=scheduler-health >> "$LOG" 2>&1 || true
  fi

  if [ "$dow" -le 5 ]; then
    for s in $SLOTS; do
      # fire in the minute of the slot (hh:mm match)
      if [ "$hm" = "$s" ]; then
        marker="logs/.sched-${day//-/}-${s/:/}"
        if [ ! -e "$marker" ]; then
          if flock -n logs/.auto_run.lock true 8>&- 2>/dev/null; then
            touch "$marker"
            log "slot $s -> scripts/auto_run.sh"
            setsid -f ./scripts/auto_run.sh </dev/null >> logs/auto_run.log 2>&1 8>&- || true
          else
            log "slot $s: auto_run lock held, skip"
          fi
        fi
      fi
    done
  fi

  # drop old markers (>7 days)
  find logs -maxdepth 1 \( -name '.sched-*' -o -name '.catchup-*' \) -mtime +7 -delete 2>/dev/null || true

  # sleep until next minute boundary (+2s) so we don't miss a slot
  sleep_for=$((60 - $(date +%S) + 2))
  [ "$sleep_for" -lt 5 ] && sleep_for=5
  [ "$sleep_for" -gt 65 ] && sleep_for=30
  sleep "$sleep_for" || sleep 30
done
