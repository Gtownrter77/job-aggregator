#!/usr/bin/env bash
# job-aggregator: bring cron + local Ollama + web UI up, and keep them up.
#
# Idempotent, safe to run repeatedly and concurrently (flock; a second copy just
# exits). Runs as the `box` user (root re-execs as box). Logs: logs/boot.log
#
# Triggers (all call this script; see scripts/autostart/ and README):
#   * D-Bus activation shim  ~/.local/share/dbus-1/services/ca.desrt.dconf.service
#                            (fires at every box boot when the desktop starts)
#   * shell startup          ~/.profile, ~/.bashrc, /etc/profile.d/zz-job-aggregator-autostart.sh
#   * crontab                @reboot + */10 watchdog
#
# What it does (each step only if needed):
#   0. re-installs the triggers above + a persisted CPU copy of Ollama (self-propagating:
#      a re-imaged root filesystem gets cron/crontab/profile.d back on the first trigger)
#   1. cron package + crontab block + cron daemon
#   2. Python .venv (rebuilt from scripts/requirements.lock.txt if missing)
#   3. Ollama binary (restored from ~/.local/opt/ollama) + `ollama serve` on :11434 + model
#   4. web UI on :8765 (/healthz must return 200; a hung UI is restarted)
#   5. catch-up: weekday slot 07:19/11:19/16:19 passed today with no successful
#      auto run since -> run scripts/auto_run.sh once (it has its own lock)
# Never sends email, never touches SMTP.
set -uo pipefail

APP=/workspace/job-aggregator
FROM=manual
for a in "$@"; do case "$a" in --from=*) FROM="${a#--from=}";; esac; done

if [ "$(id -u)" = 0 ]; then
  exec /usr/sbin/runuser -u box -- env JOBAGG_BOOT_ACTIVE=1 "$APP/scripts/boot.sh" "$@"
fi
[ "$(id -un)" = box ] || exit 0
cd "$APP" 2>/dev/null || exit 0
mkdir -p logs

export HOME=/home/box USER=box LOGNAME=box
export PATH=/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin
export JOBAGG_BOOT_ACTIVE=1 DEBIAN_FRONTEND=noninteractive LC_ALL=C.UTF-8 LANG=C.UTF-8
umask 022

LOG="$APP/logs/boot.log"
UI_URL="http://127.0.0.1:8765/healthz"
OLLAMA_URL="http://127.0.0.1:11434"
MODEL="$(sed -n 's/^[[:space:]]*model:[[:space:]]*"\{0,1\}\([^"#[:space:]]*\).*/\1/p' config.yaml 2>/dev/null | head -1)"
MODEL="${MODEL:-llama3.2:3b}"
PERSIST_OLLAMA="$HOME/.local/opt/ollama"
AUTO="$APP/scripts/autostart"

# one instance at a time; fd 9 is closed (9>&-) for everything we spawn so a
# long-lived daemon never inherits the lock
exec 9>"$APP/logs/.boot.lock"
flock -n 9 || exit 0

ACTIONS=()
log() { echo "$(date '+%F %T %Z') [$FROM] $*" >> "$LOG"; }
act() { ACTIONS+=("$*"); log "$*"; }
SUDO() { sudo -n "$@" 9>&-; }

# keep the log bounded
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 4000 ]; then
  tail -n 2000 "$LOG" > "$LOG.tmp" && mv -f "$LOG.tmp" "$LOG"
fi

# ---------------------------------------------------------------- 0. triggers
ensure_block() {   # file trigger position(top|bottom)
  local f="$1" trig="$2" pos="$3" snippet
  snippet="$(sed "s/__TRIGGER__/$trig/" "$AUTO/shell-hook.snippet")"
  [ -f "$f" ] || touch "$f"
  local have
  have="$(sed -n '/^# >>> job-aggregator autostart/,/^# <<< job-aggregator autostart <<</p' "$f" 2>/dev/null)"
  [ "$have" = "$snippet" ] && return 0
  # drop any stale copy of our block, then add the current one
  local tmp; tmp="$(mktemp)"
  sed '/^# >>> job-aggregator autostart/,/^# <<< job-aggregator autostart <<</d' "$f" > "$tmp"
  if [ "$pos" = top ]; then
    { printf '%s\n' "$snippet"; cat "$tmp"; } > "$f.jobagg.new"
  else
    { cat "$tmp"; printf '\n%s\n' "$snippet"; } > "$f.jobagg.new"
  fi
  cat "$f.jobagg.new" > "$f" && rm -f "$f.jobagg.new" "$tmp"
  act "installed shell hook in $f"
}
install_triggers() {
  # ~/.bashrc: top, before Debian's "not interactive -> return" so every bash that reads it fires
  ensure_block "$HOME/.bashrc" bashrc top
  # ~/.profile: login shells (bash -l, su - box, the agent shell's `bash -ilc`)
  ensure_block "$HOME/.profile" profile bottom
  # /etc/profile.d (system-wide, all login shells incl. root)
  local pd=/etc/profile.d/zz-job-aggregator-autostart.sh
  if ! cmp -s "$AUTO/etc-profile.d-zz-job-aggregator-autostart.sh" "$pd"; then
    SUDO install -m 0644 -o root -g root "$AUTO/etc-profile.d-zz-job-aggregator-autostart.sh" "$pd" \
      && act "installed $pd" || log "WARN could not install $pd"
  fi
  # D-Bus activation override (fires at every boot when start-desktop.sh writes dconf)
  local svc="$HOME/.local/share/dbus-1/services/ca.desrt.dconf.service"
  local shim="$HOME/.local/libexec/jobagg-dconf-service"
  if [ -x /usr/libexec/dconf-service ]; then
    if ! cmp -s "$AUTO/jobagg-dconf-service" "$shim"; then
      mkdir -p "${shim%/*}" && install -m 0755 "$AUTO/jobagg-dconf-service" "$shim" && act "installed $shim"
    fi
    if ! cmp -s "$AUTO/ca.desrt.dconf.service" "$svc"; then
      mkdir -p "${svc%/*}" && install -m 0644 "$AUTO/ca.desrt.dconf.service" "$svc" && act "installed $svc"
    fi
  fi
  # persisted CPU-only copy of Ollama in $HOME (survives a re-imaged root fs)
  if [ -x /usr/local/bin/ollama ] && [ "$(stat -c %s.%Y /usr/local/bin/ollama)" != "$(stat -c %s.%Y "$PERSIST_OLLAMA/bin/ollama" 2>/dev/null)" ]; then
    mkdir -p "$PERSIST_OLLAMA/bin" "$PERSIST_OLLAMA/lib" \
    && cp -a /usr/local/bin/ollama "$PERSIST_OLLAMA/bin/ollama.new" \
    && rm -rf "$PERSIST_OLLAMA/lib/ollama" \
    && (cd /usr/local/lib && tar --exclude=ollama/cuda_v12 --exclude=ollama/cuda_v13 --exclude=ollama/vulkan -cf - ollama) \
         | tar -xf - -C "$PERSIST_OLLAMA/lib" \
    && mv -f "$PERSIST_OLLAMA/bin/ollama.new" "$PERSIST_OLLAMA/bin/ollama" \
    && act "refreshed persisted Ollama copy in $PERSIST_OLLAMA"
  fi
}

# ---------------------------------------------------------------- 1. cron
ensure_cron() {
  if [ ! -x /usr/sbin/cron ]; then
    act "cron not installed -> apt-get install cron"
    { SUDO apt-get install -y -qq cron || { SUDO apt-get update -qq && SUDO apt-get install -y -qq cron; }; } \
      >> logs/apt-install.log 2>&1 || log "WARN apt-get install cron failed (see logs/apt-install.log)"
  fi
  [ -x /usr/sbin/cron ] || return 0
  # crontab: keep everything that isn't ours, then append the managed block
  local cur want
  cur="$(crontab -l 2>/dev/null || true)"
  want="$(printf '%s\n' "$cur" \
          | sed '/^# >>> job-aggregator (managed/,/^# <<< job-aggregator <<</d' \
          | grep -vF '/workspace/job-aggregator/scripts/' \
          | grep -vE '^# job-aggregator: local Ollama follow-up drafts' \
          | grep -vxF -e 'SHELL=/bin/bash' -e 'PATH=/usr/local/bin:/usr/bin:/bin' \
          | sed -e :a -e '/^\n*$/{$d;N;ba' -e '}')"
  want="$(printf '%s\n%s\n' "$want" "$(cat "$AUTO/crontab.block")" | sed '/./,$!d')"
  if [ "$(printf '%s\n' "$cur")" != "$(printf '%s\n' "$want")" ]; then
    printf '%s\n' "$want" | crontab - 9>&- && act "crontab updated from scripts/autostart/crontab.block" \
      || log "WARN crontab install failed"
  fi
  if ! pgrep -x cron >/dev/null 2>&1; then
    SUDO service cron start >/dev/null 2>&1 || SUDO /usr/sbin/cron >/dev/null 2>&1
    sleep 1
    pgrep -x cron >/dev/null 2>&1 && act "started cron" || log "ERROR could not start cron"
  fi
}

# ---------------------------------------------------------------- 2. venv
ensure_venv() {
  if [ -x .venv/bin/python ] && compgen -G ".venv/lib/python3*/site-packages/fastapi" >/dev/null \
     && compgen -G ".venv/lib/python3*/site-packages/jobspy" >/dev/null; then return 0; fi
  act ".venv missing/broken -> rebuilding (logs/venv-rebuild.log)"
  {
    python3 -m venv .venv \
    && .venv/bin/pip install -q --upgrade pip \
    && { .venv/bin/pip install -q -r scripts/requirements.lock.txt || .venv/bin/pip install -q -r requirements.txt; }
  } >> logs/venv-rebuild.log 2>&1 9>&- && act ".venv rebuilt" || log "ERROR .venv rebuild failed"
}

# ---------------------------------------------------------------- 3. ollama
ollama_up() { curl -fsS -m "${1:-4}" "$OLLAMA_URL/api/tags" >/dev/null 2>&1; }
ensure_ollama() {
  if [ ! -x /usr/local/bin/ollama ] && [ -x "$PERSIST_OLLAMA/bin/ollama" ]; then
    { SUDO install -m 0755 "$PERSIST_OLLAMA/bin/ollama" /usr/local/bin/ollama \
      && SUDO mkdir -p /usr/local/lib \
      && SUDO cp -a "$PERSIST_OLLAMA/lib/ollama" /usr/local/lib/; } >/dev/null 2>&1 \
      && act "restored /usr/local/bin/ollama from $PERSIST_OLLAMA" || log "WARN could not restore Ollama to /usr/local"
  fi
  local bin=""
  for b in /usr/local/bin/ollama "$PERSIST_OLLAMA/bin/ollama"; do [ -x "$b" ] && { bin="$b"; break; }; done
  if [ -z "$bin" ]; then
    if [ ! -e logs/.ollama-install-attempted ] || [ -n "$(find logs/.ollama-install-attempted -mmin +360 2>/dev/null)" ]; then
      touch logs/.ollama-install-attempted
      act "no Ollama binary anywhere -> official installer (logs/ollama-install.log)"
      curl -fsSL https://ollama.com/install.sh 2>>logs/ollama-install.log | sh >> logs/ollama-install.log 2>&1 9>&-
      [ -x /usr/local/bin/ollama ] && bin=/usr/local/bin/ollama
    fi
    [ -n "$bin" ] || { log "ERROR no Ollama binary"; return 0; }
  fi
  if ! ollama_up 4; then
    # alive-but-hung server? give it one more chance, then replace it
    local pid; pid="$(cat logs/ollama.pid 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && grep -qa 'ollama.serve' "/proc/$pid/cmdline" 2>/dev/null; then
      sleep 8
      ollama_up 8 && return 0
      act "Ollama pid $pid not answering -> restarting"; kill "$pid" 2>/dev/null; sleep 3; kill -9 "$pid" 2>/dev/null
    fi
    OLLAMA_BIN="$bin" ./scripts/ollama-bg.sh >> "$LOG" 2>&1 9>&- && act "started Ollama ($bin)" \
      || log "ERROR Ollama failed to start (logs/ollama.log)"
  fi
  # model present? (models live in ~/.ollama, which persists; pull if it ever vanishes)
  if ollama_up 4 && ! curl -fsS -m 4 "$OLLAMA_URL/api/tags" 2>/dev/null | grep -qF "\"$MODEL\""; then
    if ! pgrep -u box -f "ollama pull $MODEL" >/dev/null 2>&1; then
      act "model $MODEL missing -> ollama pull (background, logs/ollama-pull.log)"
      OLLAMA_HOST=127.0.0.1:11434 setsid -f "$bin" pull "$MODEL" >> logs/ollama-pull.log 2>&1 < /dev/null 9>&-
    fi
  fi
}

# ---------------------------------------------------------------- 4. UI
ui_ok() { [ "$(curl -s -o /dev/null -m "${1:-5}" -w '%{http_code}' "$UI_URL" 2>/dev/null)" = 200 ]; }
ensure_ui() {
  ui_ok 5 && return 0
  local pid; pid="$(cat logs/server.pid 2>/dev/null || true)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    sleep 8; ui_ok 10 && return 0
    if grep -qa 'aggregator' "/proc/$pid/cmdline" 2>/dev/null; then
      act "UI pid $pid not answering -> restarting"; kill "$pid" 2>/dev/null; sleep 3; kill -9 "$pid" 2>/dev/null
    fi
  fi
  rm -f logs/server.pid
  ./scripts/serve-bg.sh >> "$LOG" 2>&1 9>&-
  for _ in $(seq 1 30); do ui_ok 2 && { act "started UI ($(cat logs/server.pid 2>/dev/null))"; return 0; }; sleep 1; done
  log "ERROR UI did not come up (logs/server.log)"
}

# ---------------------------------------------------------------- 5. catch-up
catch_up() {
  # test knobs: JOBAGG_NOW="YYYY-mm-dd HH:MM", JOBAGG_AUTO_LOG=path, JOBAGG_CATCHUP_DRYRUN=1
  local now day dow
  now="$(date -d "${JOBAGG_NOW:-now}" +%s)" || return 0
  day="$(date -d "@$now" +%F)"; dow="$(date -d "@$now" +%u)"
  [ "$dow" -le 5 ] || return 0
  local slot="" s t
  for s in 07:19 11:19 16:19; do
    t="$(date -d "$day $s" +%s)"
    [ "$now" -ge $((t + 180)) ] && slot="$s"     # 3 min grace so cron's own run grabs the lock first
  done
  [ -n "$slot" ] || return 0
  local slot_ts; slot_ts="$(date -d "$day $slot" +%s)"
  local last_ok last_ts=0
  last_ok="$(grep -a 'auto_run end (rc=0)' "${JOBAGG_AUTO_LOG:-logs/auto_run.log}" 2>/dev/null | tail -1 | sed -n 's/^===== \([0-9-]* [0-9:]*\).*/\1/p')"
  [ -n "$last_ok" ] && last_ts="$(date -d "$last_ok" +%s 2>/dev/null || echo 0)"
  [ "$last_ts" -ge "$slot_ts" ] && return 0
  local marker="logs/.catchup-${day//-/}-${slot/:/}"
  [ -e "$marker" ] && return 0                      # at most one catch-up per slot
  if ! flock -n logs/.auto_run.lock true 9>&-; then   # a run is in progress right now
    [ -n "${JOBAGG_CATCHUP_DRYRUN:-}" ] && log "catch-up (dry-run): $slot missed but auto_run lock is held -> skip"
    return 0
  fi
  if [ -n "${JOBAGG_CATCHUP_DRYRUN:-}" ]; then
    log "catch-up (dry-run): would run scripts/auto_run.sh for missed $slot slot on $day (last ok run: ${last_ok:-never})"
    return 0
  fi
  touch "$marker"
  act "catch-up: missed $slot slot (last ok run: ${last_ok:-never}) -> scripts/auto_run.sh"
  setsid -f ./scripts/auto_run.sh < /dev/null >> logs/auto_run.log 2>&1 9>&-
}

install_triggers
ensure_cron
ensure_venv
ensure_ollama
ensure_ui
catch_up
find logs -maxdepth 1 -name '.catchup-*' -mtime +7 -delete 2>/dev/null

if [ ${#ACTIONS[@]} -eq 0 ]; then
  # quiet when healthy: one heartbeat line per hour at most
  if [ -z "$(find "$LOG" -mmin -60 2>/dev/null)" ]; then log "ok (cron, ollama, ui healthy)"; fi
fi
exit 0
