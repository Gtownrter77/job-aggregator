#!/usr/bin/env python3
"""Cross-platform runner used by the schedulers (Windows Task Scheduler, macOS launchd,
Linux systemd/cron). Pure Python, no bash needed.

    python install/run_auto.py auto  [--if-missed] [--ensure-ui] [--port N] [-- extra args for `aggregator auto`]
    python install/run_auto.py serve [--port N] [--host H] [--supervise] [--background]
    python install/run_auto.py status

* `auto` runs `python -m aggregator auto` (fetch -> rescore -> auto-qualify a few strong
  new matches as DRAFTS -> digest in logs/latest-digest.md). It never sends, approves or
  schedules email. A lock file (logs/.run_auto.lock) stops two runs from overlapping.
  `--if-missed` only runs when the most recent weekday slot (7:19, 11:19, 16:19 local time)
  hasn't been covered yet - used for "at logon/boot" triggers so a missed run catches up
  without running on every login.
* `serve` runs the web UI on 127.0.0.1 (default port from config.yaml, 8765). It exits 0
  immediately if something already answers on that port. `--supervise` restarts it if it
  crashes (used on Windows, which has no KeepAlive). `--background` starts it detached.

Logs: logs/run_auto.log (this wrapper), logs/auto_run.log (the run), logs/server.log (UI).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"
LOCK = LOGS / ".run_auto.lock"
MARKER = LOGS / "run_auto-last.json"
SLOTS = [(7, 19), (11, 19), (16, 19)]  # local wall-clock times, Monday-Friday
WEEKDAYS = {0, 1, 2, 3, 4}
SLOT_TOLERANCE = timedelta(minutes=2)
IS_WIN = os.name == "nt"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------- helpers
def _log(msg: str) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    _rotate(LOGS / "run_auto.log")
    with open(LOGS / "run_auto.log", "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    if sys.stdout is not None:  # pythonw.exe has no console
        try:
            print(line, flush=True)
        except Exception:  # noqa: BLE001
            pass


def _rotate(path: Path, max_bytes: int = 5_000_000) -> None:
    try:
        if path.exists() and path.stat().st_size > max_bytes:
            old = path.with_name(path.name + ".1")
            if old.exists():
                old.unlink()
            path.rename(old)
    except OSError:
        pass


class FileLock:
    """Non-blocking exclusive lock that works on Windows (msvcrt) and POSIX (fcntl).
    Released automatically by the OS if the process dies."""

    def __init__(self, path: Path):
        self.path = path
        self.fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+")
        try:
            if IS_WIN:
                import msvcrt

                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            self.fh = None
            return False
        self.fh.seek(0)
        self.fh.truncate()
        self.fh.write(f"{os.getpid()}\n")
        self.fh.flush()
        return True

    def release(self) -> None:
        if not self.fh:
            return
        try:
            if IS_WIN:
                import msvcrt

                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self.fh.close()
        self.fh = None


def _cfg() -> dict:
    try:
        from aggregator.config import load_config

        return load_config()
    except Exception as e:  # noqa: BLE001
        _log(f"warning: could not read config ({e}); using defaults")
        return {"server": {"port": 8765}, "llm": {"enabled": True, "ollama_url": "http://localhost:11434"}}


def _answers(url: str, timeout: float = 5) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 (localhost only)
            return 200 <= r.status < 500
    except Exception:  # noqa: BLE001
        return False


def _child_python() -> str:
    """python.exe (not pythonw.exe) for children; their output goes to log files."""
    exe = Path(sys.executable)
    if IS_WIN and exe.name.lower() == "pythonw.exe":
        cand = exe.with_name("python.exe")
        if cand.exists():
            return str(cand)
    return str(exe)


def _child_env() -> dict:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"          # Windows: read/write config, resumes and digests as UTF-8
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def _no_window_flags() -> int:
    if not IS_WIN:
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def _spawn_detached(cmd: list[str], log_path: Path, env: dict | None = None) -> int:
    """Start a process that outlives this one. Returns its pid."""
    LOGS.mkdir(parents=True, exist_ok=True)
    _rotate(log_path)
    out = open(log_path, "a", encoding="utf-8")
    kw: dict = {"cwd": str(ROOT), "stdout": out, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL,
                "env": env or _child_env(), "close_fds": True}
    if IS_WIN:
        flags = _no_window_flags() | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
        breakaway = 0x01000000  # CREATE_BREAKAWAY_FROM_JOB: survive the Task Scheduler job ending
        try:
            p = subprocess.Popen(cmd, creationflags=flags | breakaway, **kw)
        except OSError:
            p = subprocess.Popen(cmd, creationflags=flags, **kw)
    else:
        p = subprocess.Popen(cmd, start_new_session=True, **kw)
    out.close()
    return p.pid


# ---------------------------------------------------------------- schedule logic
def last_slot(now: datetime, tolerance: timedelta = SLOT_TOLERANCE) -> datetime:
    """Most recent weekday slot at or before now (+tolerance), as naive local time."""
    t = now + tolerance
    for back in range(0, 8):
        day: date = (t - timedelta(days=back)).date()
        if day.weekday() not in WEEKDAYS:
            continue
        for h, m in sorted(SLOTS, reverse=True):
            s = datetime.combine(day, dtime(h, m))
            if s <= t:
                return s
    raise RuntimeError("no slot found")  # unreachable


def last_run_started() -> datetime | None:
    try:
        return datetime.fromisoformat(json.loads(MARKER.read_text(encoding="utf-8"))["started"])
    except Exception:  # noqa: BLE001
        return None


def is_missed(now: datetime | None = None, last: datetime | None = None, *, use_marker: bool = True) -> bool:
    now = now or datetime.now()
    last = last if last is not None or not use_marker else last_run_started()
    if last is None:
        return True
    return last < last_slot(now) - SLOT_TOLERANCE


# ---------------------------------------------------------------- services
def _find_ollama() -> str | None:
    found = shutil.which("ollama")
    if found:
        return found
    cands = []
    if IS_WIN:
        la = os.environ.get("LOCALAPPDATA", "")
        cands += [Path(la) / "Programs" / "Ollama" / "ollama.exe"]
    else:
        cands += [Path("/usr/local/bin/ollama"), Path("/opt/homebrew/bin/ollama"), Path("/usr/bin/ollama"),
                  Path("/Applications/Ollama.app/Contents/Resources/ollama")]
    for c in cands:
        if c.exists():
            return str(c)
    return None


def ensure_ollama(cfg: dict) -> str:
    llm = cfg.get("llm") or {}
    if not llm.get("enabled", True):
        return "disabled in config"
    url = (llm.get("ollama_url") or "http://localhost:11434").rstrip("/")
    if _answers(url + "/api/tags", 3):
        return "already running"
    host = url.split("://", 1)[-1].split("/", 1)[0]
    if not host.startswith(("localhost", "127.0.0.1", "[::1]")):
        return f"not reachable at {url} (remote; not starting)"
    started = None
    if IS_WIN:
        app = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama app.exe"
        if app.exists():
            started = _spawn_detached([str(app)], LOGS / "ollama.log")
    elif sys.platform == "darwin" and Path("/Applications/Ollama.app").exists():
        subprocess.run(["open", "-g", "-a", "Ollama", "--args", "hidden"], check=False)
        started = "Ollama.app"
    if started is None:
        exe = _find_ollama()
        if not exe:
            return "not installed (drafts fall back to templates)"
        env = _child_env()
        env.setdefault("OLLAMA_HOST", host.replace("localhost", "127.0.0.1"))
        env.setdefault("OLLAMA_NUM_PARALLEL", "1")
        env.setdefault("OLLAMA_MAX_LOADED_MODELS", "1")
        env.setdefault("OLLAMA_KEEP_ALIVE", "15m")
        started = _spawn_detached([exe, "serve"], LOGS / "ollama.log", env)
    for _ in range(45):
        if _answers(url + "/api/tags", 2):
            return f"started ({started})"
        time.sleep(1)
    return "failed to start (drafts fall back to templates); see logs/ollama.log"


def _ui_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/"


def ensure_ui(port: int) -> str:
    if _answers(_ui_url(port)):
        return "already running"
    time.sleep(5)
    if _answers(_ui_url(port)):
        return "already running"
    if IS_WIN:  # prefer the scheduled task so there is exactly one owner of the UI
        r = subprocess.run(["schtasks", "/Run", "/TN", "JobAggregator-UI"], capture_output=True,
                           creationflags=_no_window_flags(), check=False)
        if r.returncode == 0:
            for _ in range(30):
                if _answers(_ui_url(port), 2):
                    return "started via Task Scheduler"
                time.sleep(1)
    pid = _spawn_detached([_child_python(), str(Path(__file__).resolve()), "serve", "--port", str(port), "--supervise"],
                          LOGS / "server.log")
    for _ in range(30):
        if _answers(_ui_url(port), 2):
            return f"started (pid {pid})"
        time.sleep(1)
    return f"started pid {pid} but not answering yet; see logs/server.log"


# ---------------------------------------------------------------- commands
def cmd_auto(a) -> int:
    LOGS.mkdir(parents=True, exist_ok=True)
    if a.if_missed and not is_missed():
        _log(f"auto: skipped (--if-missed): last run {last_run_started()} already covers slot {last_slot(datetime.now())}")
        return 0
    lock = FileLock(LOCK)
    if not lock.acquire():
        _log("auto: skipped: another run holds logs/.run_auto.lock")
        return 0
    try:
        cfg = _cfg()
        port = a.port or int((cfg.get("server") or {}).get("port") or 8765)
        if not a.no_ensure_ollama:
            _log(f"auto: ollama {ensure_ollama(cfg)}")
        if a.ensure_ui:
            _log(f"auto: UI {ensure_ui(port)}")
        extra = [x for x in a.extra if x != "--"]
        cmd = [_child_python(), "-m", "aggregator", "auto", *extra]
        started = datetime.now()
        _log(f"auto: start: {' '.join(cmd)}")
        if a.dry_run:
            _log("auto: --dry-run: not running it")
            return 0
        log_path = LOGS / "auto_run.log"
        _rotate(log_path)
        with open(log_path, "a", encoding="utf-8") as out:
            out.write(f"===== {started.strftime('%Y-%m-%d %H:%M:%S')} run_auto start =====\n")
            out.flush()
            rc = subprocess.call(cmd, cwd=str(ROOT), stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                 env=_child_env(), creationflags=_no_window_flags())
            out.write(f"===== {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} run_auto end (rc={rc}) =====\n")
        if rc == 0:
            MARKER.write_text(json.dumps({"started": started.isoformat(timespec="seconds"),
                                          "finished": datetime.now().isoformat(timespec="seconds")}), encoding="utf-8")
        _log(f"auto: done rc={rc} ({round((datetime.now() - started).total_seconds())} s); digest: logs/latest-digest.md")
        return rc
    finally:
        lock.release()


def cmd_serve(a) -> int:
    cfg = _cfg()
    port = a.port or int((cfg.get("server") or {}).get("port") or 8765)
    host = a.host or "127.0.0.1"
    if _answers(_ui_url(port), 3):
        _log(f"serve: something already answers on port {port}; not starting another")
        return 0
    if a.background:
        cmd = [_child_python(), str(Path(__file__).resolve()), "serve", "--port", str(port), "--host", host, "--supervise"]
        pid = _spawn_detached(cmd, LOGS / "server.log")
        _log(f"serve: started in background (pid {pid}) -> http://localhost:{port}")
        return 0
    cmd = [_child_python(), "-m", "aggregator", "serve", "--host", host, "--port", str(port)]
    failures = 0
    while True:
        t0 = time.time()
        _log(f"serve: {' '.join(cmd)}")
        log_path = LOGS / "server.log"
        _rotate(log_path)
        with open(log_path, "a", encoding="utf-8") as out:
            proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    env=_child_env(), creationflags=_no_window_flags())

            def _stop(signum, _frame, proc=proc):  # stopping the wrapper also stops the UI it started
                _log(f"serve: got signal {signum}; stopping UI (pid {proc.pid})")
                proc.terminate()
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                sys.exit(0)

            for sig in (signal.SIGTERM, signal.SIGINT) + ((signal.SIGHUP,) if hasattr(signal, "SIGHUP") else ()):
                try:
                    signal.signal(sig, _stop)
                except (ValueError, OSError):
                    pass
            rc = proc.wait()
        _log(f"serve: exited rc={rc}")
        if not a.supervise:
            return rc
        if _answers(_ui_url(port), 3):  # someone else took the port
            return 0
        failures = failures + 1 if time.time() - t0 < 60 else 1
        if failures >= 10:
            _log("serve: crashing repeatedly; giving up (see logs/server.log)")
            return 1
        time.sleep(min(60, 5 * failures))


def cmd_status(a) -> int:
    cfg = _cfg()
    port = a.port or int((cfg.get("server") or {}).get("port") or 8765)
    url = ((cfg.get("llm") or {}).get("ollama_url") or "http://localhost:11434").rstrip("/")
    print(f"UI      http://localhost:{port}  {'UP' if _answers(_ui_url(port)) else 'down'}")
    print(f"Ollama  {url}  {'UP' if _answers(url + '/api/tags', 3) else 'down'}")
    print(f"Last run started: {last_run_started() or 'never'}; latest slot: {last_slot(datetime.now())}; "
          f"missed: {is_missed()}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="run_auto.py", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd")
    pa = sub.add_parser("auto", help="run `python -m aggregator auto` (drafts only; never sends)")
    pa.add_argument("--if-missed", action="store_true", help="only run if the latest weekday slot was missed")
    pa.add_argument("--ensure-ui", action="store_true", help="also start the UI if it isn't answering")
    pa.add_argument("--no-ensure-ollama", action="store_true", help="don't try to start Ollama")
    pa.add_argument("--port", type=int)
    pa.add_argument("--dry-run", action="store_true", help="print what would run; run nothing")
    pa.add_argument("extra", nargs=argparse.REMAINDER, help="extra args for `aggregator auto` (e.g. -- --no-fetch)")
    ps = sub.add_parser("serve", help="run the web UI on 127.0.0.1")
    ps.add_argument("--port", type=int)
    ps.add_argument("--host", help="bind address (default 127.0.0.1 = this computer only)")
    ps.add_argument("--supervise", action="store_true", help="restart the UI if it crashes")
    ps.add_argument("--background", action="store_true", help="start detached and return")
    pst = sub.add_parser("status", help="show UI / Ollama / last-run status")
    pst.add_argument("--port", type=int)
    a = p.parse_args(argv)
    os.chdir(ROOT)
    if a.cmd == "serve":
        return cmd_serve(a)
    if a.cmd == "status":
        return cmd_status(a)
    if a.cmd is None:
        a = p.parse_args(["auto", *(argv or sys.argv[1:])])
    return cmd_auto(a)


if __name__ == "__main__":
    sys.exit(main())
