"""Self-contained, phone-friendly HTML snapshot of the dashboard (`python -m aggregator snapshot`).

One .html file: all CSS inline, no JavaScript, no external fonts/CSS, no server needed. Good for
opening from an email attachment or a static host on a phone. Contents: header counts, every
active lead (links, company info, follow-up status) with its 3 drafts as mailto: buttons, and the
top new jobs from both tracks. Every piece of text is HTML-escaped (Jinja autoescape); only
http(s) URLs become links.

The mailto: buttons only OPEN the user's mail app with the draft filled in; nothing is sent
from here, nothing is approved, and the DB is opened read-only in practice (SELECTs only).

`--publish` (or `dashboard.publish: true` + the `auto` run) force-pushes the page as index.html
to an orphan `gh-pages` branch built in a temp dir; the main branch is never touched.
"""
from __future__ import annotations

import html as _html
import logging
import re
import shutil
import subprocess
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import db, enrich
from .config import ROOT, resolve

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
TEMPLATES = Path(__file__).parent / "templates"
PAGES_BRANCH = "gh-pages"
_EMAIL_OK = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
MAILTO_NOTE = "Opens in your email app; nothing sends until you press Send there."


# ---------------------------------------------------------------- small helpers
def fmt_et(dt: datetime) -> str:
    """'Oct 8, 2026 10:35 AM ET'"""
    d = dt.astimezone(ET)
    return f"{d:%b} {d.day}, {d.year} {d.hour % 12 or 12}:{d:%M} {d:%p} ET"


def fmt_day(iso: str | None) -> str:
    """'2026-10-08...' -> 'Oct 8, 2026' ('' when unknown)."""
    if not iso:
        return ""
    try:
        d = date.fromisoformat(str(iso)[:10])
    except ValueError:
        return str(iso)[:10]
    return f"{d:%b} {d.day}, {d.year}"


def safe_url(u: str | None) -> str:
    """Only absolute http(s) URLs may become links (no javascript:, data:, ...)."""
    u = (u or "").strip()
    if not u or any(ch in u for ch in "\r\n\t") or " " in u:
        return ""
    try:
        parts = urlsplit(u)
    except ValueError:
        return ""
    return u if parts.scheme.lower() in ("http", "https") and parts.netloc else ""


def host(u: str | None) -> str:
    h = enrich.host_of(u) if u else ""
    return h or (u or "")


def mailto(to: str | None, subject: str, body: str) -> str:
    """RFC 6068 mailto: link. Everything percent-encoded (spaces as %20, line breaks as %0D%0A)."""
    to = (to or "").strip()
    to_part = quote(to, safe="@") if _EMAIL_OK.fullmatch(to) else ""
    body = re.sub(r"\r?\n", "\r\n", body or "")
    return f"mailto:{to_part}?subject={quote(subject or '', safe='')}&body={quote(body, safe='')}"


def excerpt(text: str | None, n: int = 300) -> str:
    """Plain-text excerpt of a posting (HTML tags/entities and markdown marks removed). Not escaped:
    the template escapes it."""
    s = _html.unescape(_html.unescape(text or ""))
    s = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", s)
    s = re.sub(r"<[^>]{0,500}>", " ", s)
    s = enrich.unescape_md(s)
    s = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", s)          # [text](url) -> text
    s = re.sub(r"[*_#>`|]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) <= n:
        return s
    cut = s[:n].rsplit(" ", 1)[0]
    return cut + "…"


def _since(now: datetime, hours: int) -> str:
    return (now.astimezone(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def _tracks(t: str | None) -> list[str]:
    return [x for x in (t or "").split(",") if x]


TRACK_LABEL = {"atlanta": "Atlanta", "remote_ai": "Remote AI"}


# ---------------------------------------------------------------- data
def _touch_state(f: dict, today: date) -> dict:
    d = date.fromisoformat(f["scheduled_for"][:10])
    pending = f["status"] in ("draft", "approved")
    delta = (d - today).days
    if not pending:
        when = f["status"]
    elif delta < 0:
        when = f"overdue {-delta} day{'s' if delta != -1 else ''}"
    elif delta == 0:
        when = "due today"
    else:
        when = f"in {delta} day{'s' if delta != 1 else ''}"
    return {**f, "date": d, "day": fmt_day(f["scheduled_for"]), "pending": pending, "due": pending and delta <= 0,
            "overdue": pending and delta < 0, "when": when, "approved": f["status"] == "approved"}


def _company(b: dict) -> dict:
    """The parts of enrich.brief() the snapshot shows, URLs vetted."""
    h = b.get("hiring_email") or None
    return {
        "status": b.get("status") or "",
        "description": b.get("description") or "",
        "website": safe_url(b.get("website")),
        "careers": safe_url(b.get("careers_url")),
        "ats_board": safe_url(b.get("ats_board_url")),
        "apply": safe_url(b.get("apply_url")),
        "apply_source": b.get("apply_url_source") or "",
        "hiring_email": (h or {}).get("email") or "",
        "hiring_source": (h or {}).get("source_url") or (h or {}).get("source") or "",
        "hiring_source_url": safe_url((h or {}).get("source_url")),
        "recruiter_name": b.get("recruiter_name") or "",
        "phone": b.get("phone") or "",
        "hq": b.get("hq") or "",
        "industry": " · ".join(x for x in (b.get("industry"), b.get("size") and f"{b['size']} employees") if x),
    }


def _job_view(j: dict, b: dict) -> dict:
    return {
        "id": j["id"], "company": j.get("company") or "(company not listed)", "title": j.get("title") or "",
        "location": j.get("location") or ("Remote" if j.get("remote") else ""), "remote": bool(j.get("remote")),
        "score": float(j.get("score") or 0), "posted": fmt_day(j.get("posted_at")) or "unknown",
        "first_seen": fmt_day(j.get("first_seen_at")), "posting": safe_url(j.get("url")),
        "tracks": [TRACK_LABEL.get(t, t) for t in _tracks(j.get("track"))], "source": j.get("source") or "",
        "excerpt": excerpt(j.get("description")), "ci": _company(b) if b else _company({}),
    }


def collect(conn, cfg: dict, now: datetime | None = None, new_limit: int = 50, new_hours: int = 72) -> dict:
    now = (now or datetime.now(timezone.utc)).astimezone(ET)
    today = now.date()
    total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    new_24h = conn.execute("SELECT COUNT(*) FROM jobs WHERE datetime(first_seen_at) >= datetime(?)",
                           (_since(now, 24),)).fetchone()[0]

    leads = []
    for r in conn.execute("SELECT l.*, j.* FROM leads l JOIN jobs j ON j.id = l.job_id WHERE l.status = 'active'"):
        r = dict(r)
        b = enrich.brief(conn, r["job_id"])
        v = _job_view(r, b)
        touches = [_touch_state(dict(f), today) for f in conn.execute(
            "SELECT id, touch, scheduled_for, status, subject, body, approved_at, sent_at FROM followups"
            " WHERE job_id = ? ORDER BY touch", (r["job_id"],))]
        to = (r.get("contact_email") or "").strip()
        for t in touches:
            t["mailto"] = mailto(to, t["subject"], t["body"])
        due = [t for t in touches if t["due"]]
        nxt = next((t for t in touches if t["pending"]), None)
        src = r.get("contact_source") or ""
        v.update(
            contact_email=to, contact_source=src.replace("enrich: ", "published on ") if src else "",
            contact_name=r.get("contact_name") or "", qualified_at=fmt_day(r.get("qualified_at")),
            qualified_by=r.get("qualified_by") or "", touches=touches, due_count=len(due),
            overdue=any(t["overdue"] for t in due), next_touch=nxt,
            sort_due=min((t["date"] for t in due), default=None),
        )
        leads.append(v)
    leads.sort(key=lambda x: (x["sort_due"] is None, -x["score"], x["company"].lower()))
    followups_due = sum(x["due_count"] for x in leads)

    # top new jobs (first seen in the last `new_hours`), not already leads, both tracks
    lead_ids = {r[0] for r in conn.execute("SELECT job_id FROM leads")}
    cols = ("id, company, title, location, remote, score, posted_at, first_seen_at, url, track, source, description")
    per_track: dict[str, list[dict]] = {}
    for t in ("atlanta", "remote_ai"):
        rows = conn.execute(
            f"SELECT {cols} FROM jobs WHERE datetime(first_seen_at) >= datetime(?) AND (',' || track || ',') LIKE ?"
            " ORDER BY score DESC, COALESCE(posted_at, first_seen_at) DESC LIMIT ?",
            (_since(now, new_hours), f"%,{t},%", new_limit + len(lead_ids) + 10)).fetchall()
        per_track[t] = [dict(r) for r in rows if r["id"] not in lead_ids]
    # half the slots per track, unused slots go to the other track; one job listed once
    picked, seen = {"atlanta": [], "remote_ai": []}, set()
    quota = {"atlanta": new_limit // 2, "remote_ai": new_limit - new_limit // 2}
    for t in quota:
        for j in per_track[t]:
            if len(picked[t]) >= quota[t]:
                break
            if j["id"] not in seen:
                picked[t].append(j)
                seen.add(j["id"])
    for t, other in (("atlanta", "remote_ai"), ("remote_ai", "atlanta")):
        spare = new_limit - len(picked["atlanta"]) - len(picked["remote_ai"])
        for j in per_track[t]:
            if spare <= 0:
                break
            if j["id"] not in seen:
                picked[t].append(j)
                seen.add(j["id"])
                spare -= 1
    new_jobs = {}
    for t, rows in picked.items():
        rows.sort(key=lambda j: -float(j["score"] or 0))
        new_jobs[t] = [_job_view(j, enrich.brief(conn, j["id"])) for j in rows]

    return {
        "generated": fmt_et(now), "generated_iso": now.isoformat(timespec="seconds"), "today": fmt_day(today.isoformat()),
        "counts": {"total": total, "new_24h": new_24h, "leads": len(leads), "followups_due": followups_due,
                   "leads_due": sum(1 for x in leads if x["due_count"]), "new_listed": sum(len(v) for v in new_jobs.values())},
        "leads": leads, "new_jobs": new_jobs, "new_hours": new_hours, "track_label": TRACK_LABEL,
        "mailto_note": MAILTO_NOTE, "applicant": (cfg.get("followups") or {}).get("applicant_name") or "",
    }


# ---------------------------------------------------------------- render / write
def _env() -> Environment:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(["html"], default=True),
                      trim_blocks=True, lstrip_blocks=True)
    env.filters["host"] = host
    return env


def render(data: dict, published: bool = False, updates_by_email: bool = False) -> str:
    return _env().get_template("snapshot.html").render(d=data, published=published, updates_by_email=updates_by_email)


def default_out(cfg: dict, now: datetime | None = None) -> Path:
    now = (now or datetime.now(timezone.utc)).astimezone(ET)
    out_dir = resolve((cfg.get("dashboard") or {}).get("out_dir") or "dist")
    return out_dir / f"Job-Dashboard-{now:%Y-%m-%d}.html"


def build(cfg: dict, out: str | Path | None = None, now: datetime | None = None,
          pages_out: str | Path | None = None) -> tuple[Path, dict]:
    """Write the snapshot to `out` (the copy you email/attach). With `pages_out`, also write the
    hosted variant (footer mentions the automatic refresh when dashboard.publish is on)."""
    now = now or datetime.now(timezone.utc)
    conn = db.connect(cfg)
    try:
        data = collect(conn, cfg, now)
    finally:
        conn.close()
    dash = cfg.get("dashboard") or {}
    by_email = bool(dash.get("updates_by_email"))
    path = Path(out) if out else default_out(cfg, now)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(data, published=False, updates_by_email=by_email), encoding="utf-8")
    if pages_out:
        # the "regenerated after each check" line is only true for the hosted copy with auto-publish on
        Path(pages_out).write_text(render(data, published=bool(dash.get("publish")), updates_by_email=by_email),
                                   encoding="utf-8")
    return path, data


def pages_path(cfg: dict) -> Path:
    return resolve((cfg.get("dashboard") or {}).get("out_dir") or "dist") / "pages-index.html"


# ---------------------------------------------------------------- publish (GitHub Pages)
def _git(args: list[str], cwd: Path, timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)


def publish(cfg: dict, html_path: Path) -> dict:
    """Force-push html_path as index.html (+ .nojekyll) to the orphan gh-pages branch.
    Built in a throwaway repo, so the working tree and every other branch are untouched. Never raises."""
    dash = cfg.get("dashboard") or {}
    branch = dash.get("branch") or PAGES_BRANCH
    if branch in ("main", "master"):
        return {"ok": False, "error": f"refusing to publish to {branch}"}
    try:
        remote = dash.get("remote") or _git(["remote", "get-url", "origin"], ROOT).stdout.strip()
        if not remote:
            return {"ok": False, "error": "no git remote 'origin'"}
        name = _git(["config", "user.name"], ROOT).stdout.strip() or "job-aggregator"
        email = _git(["config", "user.email"], ROOT).stdout.strip() or "job-aggregator@localhost"
        with tempfile.TemporaryDirectory(prefix="dash-pages-") as td:
            d = Path(td)
            shutil.copyfile(html_path, d / "index.html")
            (d / ".nojekyll").write_text("")
            steps = [["init", "-q", "-b", branch], ["add", "index.html", ".nojekyll"],
                     ["-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-q", "-m",
                      f"Dashboard snapshot {fmt_et(datetime.now(timezone.utc))}"],
                     ["-c", "credential.helper=", "-c", "credential.https://github.com.helper=!gh auth git-credential",
                      "push", "--force", "-q", remote, f"HEAD:refs/heads/{branch}"]]
            for s in steps:
                r = _git(s, d)
                if r.returncode != 0:
                    return {"ok": False, "error": f"git {s[-2] if s[0] == '-c' else s[0]} failed: {(r.stderr or r.stdout).strip()[:300]}"}
        return {"ok": True, "branch": branch, "url": dash.get("pages_url") or ""}
    except Exception as e:  # noqa: BLE001 - publishing is best effort
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def build_and_publish(cfg: dict) -> dict:
    path, data = build(cfg, pages_out=pages_path(cfg))
    res = publish(cfg, pages_path(cfg))
    res.update(path=str(path), counts=data["counts"])
    return res


def main_cli(args, cfg) -> int:
    path, data = build(cfg, out=args.out, pages_out=pages_path(cfg) if args.publish else None)
    c = data["counts"]
    print(f"Wrote {path} ({path.stat().st_size / 1024:.0f} KB): {c['total']} jobs, {c['new_24h']} new in 24h, "
          f"{c['leads']} active leads, {c['followups_due']} follow-ups due, {c['new_listed']} new jobs listed")
    if args.publish:
        res = publish(cfg, pages_path(cfg))
        if res["ok"]:
            print(f"Published to branch {res['branch']}" + (f": {res['url']}" if res.get("url") else ""))
        else:
            print(f"Publish failed (snapshot still written): {res['error']}")
            return 1
    return 0
