#!/usr/bin/env bash
# Job Aggregator - one-command installer for macOS and Linux.
#
#   bash install/install.sh                       full install (re-runnable)
#   bash install/install.sh /path/personal-pack.zip   use this personal pack
#   bash install/install.sh --uninstall           remove schedules + shortcut (keeps all data)
#
# Options:
#   (pack = config.local.yaml + optional data/jobs.db; resumes/ ships with the repo)
#   --no-schedule      don't register LaunchAgents / systemd units / cron (and don't start the UI)
#   --no-ollama        skip Ollama entirely (follow-up drafts then use templates)
#   --no-model         install/start Ollama but don't pull the model
#   --model NAME       Ollama model to pull (default: llm.model from config, llama3.2:3b)
#   --port N           UI port (default: server.port from config, 8765)
#   --units-dir DIR    also write the generated LaunchAgent/systemd files to DIR (for review)
#   --dry-run          print what would happen and write unit files to --units-dir; change nothing
#   --force-cron       allow the cron fallback even if a crontab already runs this app
#   -y, --yes          answer "yes" to prompts (e.g. use the small model on low-RAM machines)
#
# Sets up: Python 3.10+, .venv + requirements, your personal pack (config.local.yaml,
# resumes/profile.*, optional data/jobs.db), Ollama + llama3.2:3b (free, local, no API keys),
# runs `auto` on weekdays at 7:19, 11:19, 16:19 local time and at login (missed runs catch
# up), keeps the UI at http://localhost:8765 running, and puts a shortcut on the Desktop.
# It never sends email.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DO_SCHEDULE=1 DO_OLLAMA=1 DO_MODEL=1 DRY_RUN=0 UNINSTALL=0 FORCE_YES=0 FORCE_CRON=0
PACK_ARG="" MODEL="" PORT="" UNITS_DIR=""
MODEL_SMALL="llama3.2:1b"
LABEL_UI="com.jobaggregator.ui"
LABEL_AUTO="com.jobaggregator.auto"
UNIT_UI="job-aggregator-ui"
UNIT_AUTO="job-aggregator-auto"
UNIT_OLLAMA="job-aggregator-ollama"
CRON_BEGIN="# >>> job-aggregator (install/install.sh) >>>"
CRON_END="# <<< job-aggregator (install/install.sh) <<<"
PY_MAC_PKG="https://www.python.org/ftp/python/3.12.10/python-3.12.10-macos11.pkg"

while [ $# -gt 0 ]; do
  case "$1" in
    --no-schedule) DO_SCHEDULE=0 ;;
    --no-ollama)   DO_OLLAMA=0 ;;
    --no-model)    DO_MODEL=0 ;;
    --dry-run)     DRY_RUN=1 ;;
    --uninstall)   UNINSTALL=1 ;;
    --force-cron)  FORCE_CRON=1 ;;
    -y|--yes)      FORCE_YES=1 ;;
    --model)       MODEL="${2:?--model needs a value}"; shift ;;
    --port)        PORT="${2:?--port needs a value}"; shift ;;
    --units-dir)   UNITS_DIR="${2:?--units-dir needs a value}"; shift ;;
    -h|--help)     sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*)            echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
    *)             PACK_ARG="$1" ;;
  esac
  shift
done

say()  { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
run()  { if [ "$DRY_RUN" = 1 ]; then say "  [dry-run] $*"; else "$@"; fi; }
sudo_() { if [ "$(id -u)" = 0 ]; then "$@"; elif have sudo; then sudo "$@"; else "$@"; fi; }

case "$(uname -s)" in
  Darwin) OS_KIND=mac ;;
  Linux)  OS_KIND=linux ;;
  *) die "Unsupported OS $(uname -s). On Windows run install\\install.bat" ;;
esac

ask_yn() {  # $1 prompt; --yes => yes; no terminal => no
  [ "$FORCE_YES" = 1 ] && return 0
  [ -t 0 ] || return 1
  local ans=""
  read -r -p "$1 [y/N] " ans || return 1
  case "$ans" in y|Y|yes|Yes|YES) return 0 ;; *) return 1 ;; esac
}

VPY="$ROOT/.venv/bin/python"      # interpreter the schedulers call
RUNNER="$ROOT/install/run_auto.py"

# =========================================================================== uninstall
if [ "$UNINSTALL" = 1 ]; then
  step "Removing schedules and shortcut (your data, settings, venv and Ollama are kept)"
  if [ "$OS_KIND" = mac ]; then
    for L in "$LABEL_UI" "$LABEL_AUTO"; do
      plist="$HOME/Library/LaunchAgents/$L.plist"
      if [ -f "$plist" ]; then
        run launchctl bootout "gui/$(id -u)/$L" 2>/dev/null || run launchctl unload "$plist" 2>/dev/null || true
        run rm -f "$plist"; say "  removed $plist"
      fi
    done
    run rm -f "$HOME/Desktop/Job Aggregator.webloc"
  else
    if have systemctl && systemctl --user show-environment >/dev/null 2>&1; then
      for U in "$UNIT_AUTO.timer" "$UNIT_AUTO.service" "$UNIT_UI.service" "$UNIT_OLLAMA.service"; do
        if [ -f "$HOME/.config/systemd/user/$U" ]; then
          run systemctl --user disable --now "$U" 2>/dev/null || true
          run rm -f "$HOME/.config/systemd/user/$U"; say "  removed $U"
        fi
      done
      run systemctl --user daemon-reload || true
    fi
    if have crontab && crontab -l 2>/dev/null | grep -qF "$CRON_BEGIN"; then
      if [ "$DRY_RUN" = 1 ]; then say "  [dry-run] remove crontab block"; else
        crontab -l 2>/dev/null | awk -v b="$CRON_BEGIN" -v e="$CRON_END" '$0==b{s=1;next} $0==e{s=0;next} !s' | crontab -
        say "  removed crontab block"
      fi
    fi
    run rm -f "$HOME/Desktop/Job Aggregator.desktop" "$HOME/.local/share/applications/job-aggregator.desktop"
  fi
  if have pkill; then run pkill -f "$RUNNER serve" 2>/dev/null || true; fi
  say "Done. Data kept in $ROOT (data/, logs/, config.local.yaml, resumes/)."
  exit 0
fi

# =========================================================================== macOS: protected folders
# launchd jobs can't read ~/Desktop, ~/Documents or ~/Downloads without extra privacy
# permissions, so the scheduled runs would fail there. Offer to move to ~/job-aggregator.
if [ "$OS_KIND" = mac ] && [ "$DRY_RUN" = 0 ]; then
  case "$ROOT/" in
    "$HOME/Desktop/"*|"$HOME/Documents/"*|"$HOME/Downloads/"*)
      DEST="$HOME/job-aggregator"
      warn "This folder is inside Desktop/Documents/Downloads, which macOS blocks for background jobs."
      if [ ! -e "$DEST" ] && ask_yn "Copy it to $DEST and continue from there? (recommended)"; then
        PACK_FOUND="$PACK_ARG"
        if [ -z "$PACK_FOUND" ]; then
          for c in "$ROOT/personal-pack.zip" "$(dirname "$ROOT")/personal-pack.zip" "$ROOT/dist/personal-pack.zip"; do
            [ -f "$c" ] && { PACK_FOUND="$c"; break; }
          done
        fi
        ditto "$ROOT" "$DEST"
        rm -rf "$DEST/.venv"
        say "Copied to $DEST. Continuing there..."
        ARGS=()
        [ "$DO_SCHEDULE" = 0 ] && ARGS+=(--no-schedule)
        [ "$DO_OLLAMA" = 0 ] && ARGS+=(--no-ollama)
        [ "$DO_MODEL" = 0 ] && ARGS+=(--no-model)
        [ "$FORCE_YES" = 1 ] && ARGS+=(--yes)
        [ -n "$MODEL" ] && ARGS+=(--model "$MODEL")
        [ -n "$PORT" ] && ARGS+=(--port "$PORT")
        [ -n "$PACK_FOUND" ] && ARGS+=("$PACK_FOUND")
        exec bash "$DEST/install/install.sh" ${ARGS[@]+"${ARGS[@]}"}
      else
        warn "Continuing here; if scheduled runs fail, move the folder to your home folder and re-run."
      fi
      ;;
  esac
fi

# =========================================================================== Python 3.10+
find_python() {
  local c
  for c in python3.12 python3.13 python3.11 python3.10 python3 \
           /opt/homebrew/bin/python3 /usr/local/bin/python3 python3.14; do
    if have "$c" && "$c" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)' 2>/dev/null; then
      command -v "$c"; return 0
    fi
  done
  return 1
}

step "Python 3.10+"
PY="$(find_python || true)"
if [ -z "$PY" ]; then
  if [ "$OS_KIND" = mac ]; then
    if have brew; then
      say "  installing Python with Homebrew..."
      run brew install python@3.12
    elif ask_yn "Python 3.10+ is missing. Download and install Python 3.12 from python.org (asks for your Mac password)?"; then
      tmp="$(mktemp -d "${TMPDIR:-/tmp}/jobagg.XXXXXX")"
      run curl -fL -o "$tmp/python.pkg" "$PY_MAC_PKG"
      run sudo installer -pkg "$tmp/python.pkg" -target /
      export PATH="/Library/Frameworks/Python.framework/Versions/3.12/bin:$PATH"
    else
      die "Install Python 3.12 from https://www.python.org/downloads/macos/ (or Homebrew: https://brew.sh), then run this again."
    fi
  else
    if have apt-get; then
      run sudo_ apt-get update
      run sudo_ env DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv python3-pip curl
    elif have dnf; then
      run sudo_ dnf install -y python3 python3-pip curl
    elif have zypper; then
      run sudo_ zypper --non-interactive install python3 python3-pip curl
    elif have pacman; then
      run sudo_ pacman -Sy --noconfirm python python-pip curl
    else
      die "Install Python 3.10+ with your package manager, then run this again."
    fi
  fi
  PY="$(find_python || true)"
  [ -n "$PY" ] || [ "$DRY_RUN" = 1 ] || die "Python 3.10+ still not found. Install it from https://www.python.org/downloads/ and run this again."
fi
PY="${PY:-python3}"
say "  using $PY ($("$PY" -V 2>&1 || echo '?'))"
if have git; then say "  git: $(git --version)"; else say "  git: not installed (optional)"; fi

# Debian/Ubuntu ship venv/ensurepip separately.
if [ "$OS_KIND" = linux ] && ! "$PY" -c 'import ensurepip, venv' >/dev/null 2>&1; then
  if have apt-get; then
    PYVER="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    run sudo_ env DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv "python${PYVER}-venv" || \
      run sudo_ env DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv
  else
    warn "Python's venv module is missing; install your distro's python3-venv package."
  fi
fi

# =========================================================================== venv + requirements
step "Virtual environment + Python packages"
if [ "$DRY_RUN" = 1 ]; then
  say "  [dry-run] $PY -m venv .venv ; .venv/bin/python -m pip install -r requirements.txt"
else
  if [ -x "$VPY" ] && ! "$VPY" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)' >/dev/null 2>&1; then
    warn "existing .venv is broken or too old (copied from another computer?); recreating it"
    rm -rf .venv
  fi
  [ -x "$VPY" ] || "$PY" -m venv .venv
  "$VPY" -m pip install --disable-pip-version-check -q --upgrade pip
  "$VPY" -m pip install --disable-pip-version-check -r requirements.txt
  say "  ready: $ROOT/.venv"
fi
mkdir -p logs data

# =========================================================================== personal pack
step "Personal pack (your private settings + saved jobs)"
if [ "$DRY_RUN" = 1 ]; then
  say "  [dry-run] $VPY install/import_personal_pack.py ${PACK_ARG:-(search next to repo / Downloads)}"
elif [ -n "$PACK_ARG" ]; then
  [ -f "$PACK_ARG" ] || die "personal pack not found: $PACK_ARG"
  "$VPY" install/import_personal_pack.py "$PACK_ARG"
elif "$VPY" install/import_personal_pack.py --find >/dev/null 2>&1; then
  "$VPY" install/import_personal_pack.py
else
  say "  no personal-pack.zip found next to the repo or in Downloads"
fi
if [ ! -f config.local.yaml ] && [ "$DRY_RUN" = 0 ]; then
  cp config.local.example.yaml config.local.yaml
  warn "Created config.local.yaml from the example: open it and fill in your name, phone and email."
fi
if [ ! -f resumes/profile_text.txt ] && [ "$DRY_RUN" = 0 ]; then
  warn "No resumes/profile_text.txt: add your resume as plain text there so jobs get match scores."
fi

cfg_get() {  # $1 = python expression on cfg; prints value or $2
  "$VPY" -c "from aggregator.config import load_config; cfg = load_config(); print($1)" 2>/dev/null || echo "$2"
}
[ -n "$PORT" ] || PORT="$(cfg_get 'cfg["server"]["port"]' 8765)"
CFG_MODEL="$(cfg_get 'cfg["llm"]["model"]' llama3.2:3b)"
OLLAMA_URL="$(cfg_get 'cfg["llm"]["ollama_url"].rstrip("/")' http://localhost:11434)"
URL="http://localhost:$PORT"

# =========================================================================== Ollama
ollama_up() { curl -fsS -m 3 "$OLLAMA_URL/api/tags" >/dev/null 2>&1; }
ram_gb() {
  if [ "$OS_KIND" = mac ]; then
    echo $(( $(sysctl -n hw.memsize 2>/dev/null || echo 0) / 1073741824 ))
  else
    awk '/^MemTotal:/ {printf "%d\n", $2/1048576}' /proc/meminfo 2>/dev/null || echo 0
  fi
}

if [ "$DO_OLLAMA" = 1 ]; then
  step "Ollama (local AI for drafts; free and open source, no API keys)"
  if have ollama || [ -d /Applications/Ollama.app ]; then
    say "  already installed"
  elif [ "$OS_KIND" = mac ] && have brew; then
    run brew install --cask ollama-app
  else
    say "  installing with the official script (https://ollama.com/install.sh)..."
    if [ "$DRY_RUN" = 1 ]; then say "  [dry-run] curl -fsSL https://ollama.com/install.sh | sh"
    else curl -fsSL https://ollama.com/install.sh | sh; fi
  fi
  if [ "$DRY_RUN" = 0 ]; then
    if ollama_up; then
      say "  running at $OLLAMA_URL"
    else
      if [ "$OS_KIND" = linux ] && have systemctl && systemctl is-enabled ollama >/dev/null 2>&1; then
        sudo_ systemctl start ollama || true
      fi
      ollama_up || "$VPY" -c "import sys; sys.path.insert(0, 'install'); import run_auto; print('  ollama:', run_auto.ensure_ollama(run_auto._cfg()))"
    fi
    # Linux: make sure it comes back after a reboot/login.
    if [ "$OS_KIND" = linux ] && [ "$DO_SCHEDULE" = 1 ]; then
      if have systemctl && systemctl is-enabled ollama >/dev/null 2>&1; then
        say "  autostart: system service 'ollama' is enabled"
      elif have systemctl && systemctl --user show-environment >/dev/null 2>&1 && have ollama; then
        mkdir -p "$HOME/.config/systemd/user"
        cat >"$HOME/.config/systemd/user/$UNIT_OLLAMA.service" <<EOF
[Unit]
Description=Ollama for Job Aggregator (local AI, no paid APIs)

[Service]
ExecStart="$(command -v ollama)" serve
Environment=OLLAMA_HOST=127.0.0.1:11434 OLLAMA_NUM_PARALLEL=1 OLLAMA_MAX_LOADED_MODELS=1 OLLAMA_KEEP_ALIVE=15m
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
        systemctl --user daemon-reload
        if ollama_up; then   # already running some other way: just start it at next login
          systemctl --user enable "$UNIT_OLLAMA.service" || true
        else
          systemctl --user enable --now "$UNIT_OLLAMA.service" || true
        fi
        say "  autostart: systemd --user unit $UNIT_OLLAMA.service"
      else
        say "  autostart: each scheduled run starts Ollama if it isn't running"
      fi
    fi
  fi

  if [ "$DO_MODEL" = 1 ]; then
    GB="$(ram_gb)"
    if [ -z "$MODEL" ]; then
      MODEL="$CFG_MODEL"
      if [ "$GB" -gt 0 ] && [ "$GB" -lt 8 ] && [ "$MODEL" != "$MODEL_SMALL" ]; then
        warn "This computer has ~${GB} GB RAM. $MODEL works best with 8 GB+; $MODEL_SMALL is lighter (shorter, plainer drafts)."
        if ask_yn "Use the smaller $MODEL_SMALL instead?"; then MODEL="$MODEL_SMALL"; fi
      fi
    fi
    say "  RAM: ${GB} GB; model: $MODEL"
    if [ "$DRY_RUN" = 1 ]; then
      say "  [dry-run] ollama pull $MODEL"
    elif ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx -e "$MODEL" -e "$MODEL:latest"; then
      say "  $MODEL already downloaded"
    else
      say "  downloading $MODEL (about 1-2 GB, one time)..."
      ollama pull "$MODEL" || warn "Could not download $MODEL now; drafts use templates until: ollama pull $MODEL"
    fi
    if [ "$MODEL" != "$CFG_MODEL" ] && [ "$DRY_RUN" = 0 ]; then
      if ! grep -qE '^llm:' config.local.yaml; then
        printf '\n# set by install/install.sh\nllm:\n  model: "%s"\n' "$MODEL" >> config.local.yaml
        say "  config.local.yaml: llm.model = $MODEL"
      else
        warn "config.local.yaml already has an llm: section; set  model: \"$MODEL\"  in it yourself."
      fi
    fi
  fi
else
  step "Skipping Ollama (--no-ollama): drafts will use the built-in templates"
fi

# =========================================================================== schedule files
xml_esc() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }

write_plist() {  # $1 label  $2 out  $3 kind(ui|auto)  $4.. ProgramArguments
  local label="$1" out="$2" kind="$3" a h d; shift 3
  {
    echo '<?xml version="1.0" encoding="UTF-8"?>'
    echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
    echo '<plist version="1.0">'
    echo '<dict>'
    echo "  <key>Label</key><string>$label</string>"
    echo "  <key>WorkingDirectory</key><string>$(xml_esc "$ROOT")</string>"
    echo '  <key>ProgramArguments</key>'
    echo '  <array>'
    for a in "$@"; do echo "    <string>$(xml_esc "$a")</string>"; done
    echo '  </array>'
    echo '  <key>EnvironmentVariables</key>'
    echo '  <dict>'
    echo '    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>'
    echo '    <key>PYTHONUTF8</key><string>1</string>'
    echo '  </dict>'
    echo "  <key>StandardOutPath</key><string>$(xml_esc "$ROOT/logs/launchd-$kind.log")</string>"
    echo "  <key>StandardErrorPath</key><string>$(xml_esc "$ROOT/logs/launchd-$kind.log")</string>"
    echo '  <key>RunAtLoad</key><true/>'
    if [ "$kind" = ui ]; then
      echo '  <key>KeepAlive</key>'
      echo '  <dict><key>SuccessfulExit</key><false/></dict>'
      echo '  <key>ThrottleInterval</key><integer>30</integer>'
    else
      echo '  <key>StartCalendarInterval</key>'
      echo '  <array>'
      for h in 7 11 16; do
        for d in 1 2 3 4 5; do
          echo "    <dict><key>Weekday</key><integer>$d</integer><key>Hour</key><integer>$h</integer><key>Minute</key><integer>19</integer></dict>"
        done
      done
      echo '  </array>'
      echo '  <key>Nice</key><integer>10</integer>'
    fi
    echo '</dict>'
    echo '</plist>'
  } >"$out"
}

sd_q() { printf '"%s"' "$(printf '%s' "$1" | sed 's/%/%%/g')"; }  # systemd-quote a path

write_units() {  # $1 = directory
  local dir="$1"
  mkdir -p "$dir"
  if [ "$OS_KIND" = mac ]; then
    write_plist "$LABEL_UI"   "$dir/$LABEL_UI.plist"   ui   "$VPY" "$RUNNER" serve --port "$PORT" --supervise
    write_plist "$LABEL_AUTO" "$dir/$LABEL_AUTO.plist" auto "$VPY" "$RUNNER" auto --if-missed --port "$PORT"
    return
  fi
  cat >"$dir/$UNIT_UI.service" <<EOF
[Unit]
Description=Job Aggregator web UI on http://localhost:$PORT
After=network.target

[Service]
Type=simple
WorkingDirectory=$(printf '%s' "$ROOT" | sed 's/%/%%/g')
Environment=PYTHONUTF8=1
ExecStart=$(sd_q "$VPY") $(sd_q "$RUNNER") serve --port $PORT
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF
  cat >"$dir/$UNIT_AUTO.service" <<EOF
[Unit]
Description=Job Aggregator unattended run (fetch + follow-up DRAFTS; never sends email)
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$(printf '%s' "$ROOT" | sed 's/%/%%/g')
Environment=PYTHONUTF8=1
Environment=PATH=/usr/local/bin:/usr/bin:/bin
ExecStart=$(sd_q "$VPY") $(sd_q "$RUNNER") auto --if-missed --port $PORT
Nice=10
TimeoutStartSec=2h
EOF
  cat >"$dir/$UNIT_AUTO.timer" <<EOF
[Unit]
Description=Job Aggregator: weekdays 7:19, 11:19, 16:19 local time, at login, and catch-up after downtime

[Timer]
OnCalendar=Mon..Fri *-*-* 07:19:00
OnCalendar=Mon..Fri *-*-* 11:19:00
OnCalendar=Mon..Fri *-*-* 16:19:00
OnStartupSec=2min
Persistent=true
Unit=$UNIT_AUTO.service

[Install]
WantedBy=timers.target
EOF
}

cron_block() {
  local q_vpy q_run
  q_vpy="\"$VPY\""; q_run="\"$RUNNER\""
  cat <<EOF
$CRON_BEGIN
19 7,11,16 * * 1-5  $q_vpy $q_run auto --if-missed --ensure-ui --port $PORT >/dev/null 2>&1
@reboot             sleep 60; $q_vpy $q_run serve --background --port $PORT >/dev/null 2>&1; $q_vpy $q_run auto --if-missed --port $PORT >/dev/null 2>&1
*/15 * * * *        $q_vpy $q_run serve --background --port $PORT >/dev/null 2>&1
$CRON_END
EOF
}

install_cron() {
  have crontab || { warn "no systemd --user and no crontab: start the app yourself with  $VPY $RUNNER auto"; return 0; }
  local cur
  cur="$(crontab -l 2>/dev/null || true)"
  if [ "$FORCE_CRON" = 0 ] && printf '%s\n' "$cur" | grep -v -F "$RUNNER" | grep -qE 'auto_run\.sh|aggregator (auto|fetch)|scripts/boot\.sh'; then
    warn "Your crontab already runs this app (auto_run.sh / boot.sh). Not adding a duplicate schedule."
    warn "Use --force-cron to add it anyway."
    return 0
  fi
  { printf '%s\n' "$cur" | awk -v b="$CRON_BEGIN" -v e="$CRON_END" '$0==b{s=1;next} $0==e{s=0;next} !s'
    cron_block; } | crontab -
  say "  cron: weekdays 7:19/11:19/16:19 + @reboot catch-up + UI watchdog every 15 min"
}

# =========================================================================== schedule
if [ -n "$UNITS_DIR" ] || [ "$DRY_RUN" = 1 ]; then
  UNITS_DIR="${UNITS_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/jobagg-units.XXXXXX")}"
  write_units "$UNITS_DIR"
  [ "$OS_KIND" = linux ] && cron_block >"$UNITS_DIR/crontab.block"
  say "  generated schedule files in $UNITS_DIR"
fi

if [ "$DO_SCHEDULE" = 1 ] && [ "$DRY_RUN" = 0 ]; then
  step "Schedule: weekdays 7:19 / 11:19 / 16:19 local time + at login (missed runs catch up); UI always on"
  if [ "$OS_KIND" = mac ]; then
    AG="$HOME/Library/LaunchAgents"
    mkdir -p "$AG"
    write_units "$AG"
    for L in "$LABEL_UI" "$LABEL_AUTO"; do
      launchctl bootout "gui/$(id -u)/$L" >/dev/null 2>&1 || true
      launchctl bootstrap "gui/$(id -u)" "$AG/$L.plist" 2>/dev/null || launchctl load -w "$AG/$L.plist"
      say "  LaunchAgent loaded: $AG/$L.plist"
    done
    say "  (macOS may show a 'Background Items Added' notice for python - that's this app.)"
  elif have systemctl && systemctl --user show-environment >/dev/null 2>&1; then
    UD="$HOME/.config/systemd/user"
    write_units "$UD"
    systemctl --user daemon-reload
    systemctl --user enable --now "$UNIT_UI.service"
    systemctl --user enable --now "$UNIT_AUTO.timer"
    systemctl --user start --no-block "$UNIT_AUTO.service" || true   # first run now if a slot was missed
    say "  systemd --user: $UNIT_UI.service (always on) + $UNIT_AUTO.timer (Persistent=true)"
    say "  status: systemctl --user list-timers $UNIT_AUTO.timer"
  else
    warn "systemd --user isn't available here; using cron instead"
    install_cron
    "$VPY" "$RUNNER" serve --background --port "$PORT" || true
  fi
elif [ "$DO_SCHEDULE" = 0 ]; then
  step "Skipping schedule (--no-schedule). Run by hand:  $VPY $RUNNER auto   |   UI:  $VPY $RUNNER serve"
fi

# =========================================================================== desktop shortcut
step "Desktop shortcut"
if [ "$DRY_RUN" = 1 ]; then
  say "  [dry-run] shortcut to $URL on the Desktop"
elif [ "$OS_KIND" = mac ]; then
  mkdir -p "$HOME/Desktop"
  cat >"$HOME/Desktop/Job Aggregator.webloc" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>URL</key><string>$URL</string></dict></plist>
EOF
  say "  ~/Desktop/Job Aggregator.webloc -> $URL"
else
  DESK="$HOME/Desktop"
  have xdg-user-dir && DESK="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$DESK")"
  mkdir -p "$HOME/.local/share/applications"
  ENTRY="[Desktop Entry]
Type=Application
Name=Job Aggregator
Comment=Open the job search UI ($URL)
Exec=sh -c '\"$VPY\" \"$RUNNER\" serve --background --port $PORT; sleep 2; xdg-open $URL'
Icon=applications-office
Terminal=false
Categories=Office;"
  printf '%s\n' "$ENTRY" >"$HOME/.local/share/applications/job-aggregator.desktop"
  if [ -d "$DESK" ]; then
    printf '%s\n' "$ENTRY" >"$DESK/Job Aggregator.desktop"
    chmod +x "$DESK/Job Aggregator.desktop"
    if have gio; then gio set "$DESK/Job Aggregator.desktop" metadata::trusted true 2>/dev/null || true; fi
    say "  $DESK/Job Aggregator.desktop -> $URL"
  else
    say "  app menu: Job Aggregator -> $URL"
  fi
fi

# =========================================================================== done
say ""
say "=================================================================="
say " Job Aggregator is set up."
say "   Open:      $URL"
if [ "$DO_SCHEDULE" = 1 ] && [ "$DRY_RUN" = 0 ]; then
say "   Automatic: weekdays 7:19, 11:19, 16:19 + at login (catch-up)."
say "              The first check may start within a few minutes."
fi
say "   Results:   $ROOT/logs/latest-digest.md (and the UI)"
say "   Email:     NOTHING is ever sent automatically - drafts only."
say "   Remove schedules later:  bash install/install.sh --uninstall"
say "=================================================================="
