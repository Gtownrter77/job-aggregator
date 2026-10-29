# Autostart / self-healing (Cursor box)

`scripts/boot.sh` brings **cron + Ollama (:11434) + the web UI (:8765)** up and keeps
them up. It is idempotent, silent when healthy (~0.3 s), and single-instance (`flock`
on `logs/.boot.lock`). Log: `logs/boot.log` (each line is tagged with its trigger).

## How the box boots (why it is wired this way)
* PID 1 is `tini -- /pod-daemon`; there is **no systemd / SysV init**, so `/etc/init.d`,
  `/etc/rc*.d`, `rc.local` and `@reboot`-without-cron never run on their own.
  `/usr/local/bin/start-sand-box` (platform-owned) starts the exec daemon, the desktop
  (`start-desktop.sh`) and platform daemons. Nothing in it calls user hooks.
* A box can **hibernate/resume** (processes survive; nothing to do) or **cold boot**.
  On a cold boot the root filesystem comes from the image and only the box store is
  restored: `/workspace` (minus `.venv/`, `node_modules/`, caches…), `/home/box`
  (minus `.cache/`, `.npm/`…), `sand-data`, `cli-config`, Chrome profile.
  So `/etc`, `/usr/local/bin/ollama`, the `cron` package and the user crontab can
  disappear; `boot.sh` re-creates all of them.

## Triggers (all just run `scripts/boot-hook.sh` -> detached `boot.sh`)
| Trigger | File | Fires when | Survives cold boot |
|---|---|---|---|
| D-Bus activation shim | `~/.local/share/dbus-1/services/ca.desrt.dconf.service` -> `~/.local/libexec/jobagg-dconf-service` | every boot: `start-desktop.sh` runs `dconf write`, D-Bus activates dconf through the shim, which fires the hook and then `exec`s the real `/usr/libexec/dconf-service` | yes (in $HOME) |
| `~/.bashrc` (top) | `~/.bashrc` | any interactive bash, any login bash (via `~/.profile`), the agent shell's `bash -ilc` init | yes |
| `~/.profile` | `~/.profile` | login shells (`bash -l`, `su - box`) | yes |
| `/etc/profile.d` | `/etc/profile.d/zz-job-aggregator-autostart.sh` | every login shell incl. root (`sudo -i`) | re-installed by boot.sh |
| cron `@reboot` | user crontab | whenever cron starts fresh | re-installed by boot.sh |
| cron watchdog | user crontab `*/10 * * * *` | every 10 min | re-installed by boot.sh |

`boot-hook.sh` is rate-limited (one spawn per 60 s per uid) and never blocks a shell.

## What boot.sh does (each step only if needed)
0. re-installs every trigger above + a persisted CPU-only Ollama copy in `~/.local/opt/ollama`
1. `apt-get install cron` if missing; managed crontab block from `scripts/autostart/crontab.block`; starts cron
2. rebuilds `.venv` from `scripts/requirements.lock.txt` if missing
3. restores `/usr/local/bin/ollama` (+`/usr/local/lib/ollama`) from the persisted copy
   (official installer only as last resort); starts `ollama serve`; pulls the model if missing;
   restarts a hung server
4. starts the UI if `/healthz` is not 200; restarts a hung UI
5. **catch-up**: on weekdays, if the latest 07:19/11:19/16:19 slot (+3 min grace) has passed
   today and the last `auto_run end (rc=0)` in `logs/auto_run.log` is older than it, runs
   `scripts/auto_run.sh` once for that slot (marker `logs/.catchup-YYYYMMDD-HHMM`; skipped while
   `logs/.auto_run.lock` is held). Sends due drafts with a confirmed hiring email only when followups.sending_enabled is true (kill switch; default off).

Edit the schedule in `scripts/autostart/crontab.block` (boot.sh syncs it into the crontab;
direct `crontab -e` edits inside the managed block are reverted).

Test knobs (catch-up only): `JOBAGG_CATCHUP_DRYRUN=1 JOBAGG_NOW="2026-10-08 08:00" JOBAGG_AUTO_LOG=/tmp/x.log scripts/boot.sh --from=test`


## Cron-free scheduler (when apt/cron is broken)

On this Cursor box, Debian mirrors sometimes return HTTP 500 and `apt-get install cron`
fails. `boot.sh` caches that failure for 6 hours (`logs/.apt-cron-failed`) so it does
not hammer apt every boot, and starts **`scripts/scheduler-bg.sh`** instead:

* Long-lived daemon (pid in `logs/scheduler.pid`, log `logs/scheduler.log`)
* Every minute: if a weekday America/New_York slot **07:19 / 11:19 / 16:19** is due and
  no successful `auto_run` has finished for that slot yet, launches `scripts/auto_run.sh`
* Every 10 minutes: re-runs `scripts/boot.sh --from=scheduler` (health / catch-up)

Manual start: `./scripts/start-scheduler.sh`  
Stop: `kill "$(cat logs/scheduler.pid)"`

The catch-up hook in boot.sh still works even without cron or the scheduler.

## Remove
`crontab -l | sed '/^# >>> job-aggregator (managed/,/^# <<< job-aggregator <<</d' | crontab -`;
delete the `job-aggregator autostart` blocks in `~/.bashrc` / `~/.profile`;
`sudo rm /etc/profile.d/zz-job-aggregator-autostart.sh`;
`rm ~/.local/share/dbus-1/services/ca.desrt.dconf.service ~/.local/libexec/jobagg-dconf-service`.
