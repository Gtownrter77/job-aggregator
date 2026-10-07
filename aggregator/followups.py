"""Qualified leads + 3-touch follow-up DRAFT queue.

Safety model: this module only creates/edits rows. It never sends email.
Sending lives in sender.py and only touches rows with status='approved'.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from .compose import MARKER_RE, generate_sequence, template_sequence

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# addresses that are clearly not a person/recruiting inbox
_BAD_EMAIL = re.compile(r"(no-?reply|donotreply|example\.(com|org)|privacy|accommodation|ada@|eeo|\.(png|jpg|gif)$)", re.I)
UNSENT = ("draft", "approved")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def schedule(start: date, cfg: dict) -> list[date]:
    out = []
    for d in cfg["followups"]["schedule_days"][:3]:
        day = start + timedelta(days=int(d))
        if cfg["followups"].get("skip_weekends"):
            while day.weekday() >= 5:  # Sat/Sun -> Monday
                day += timedelta(days=1)
        out.append(day)
    return out


def contact_from_posting(description: str | None) -> str | None:
    """Email literally written in the posting text (no guessing, no external lookup)."""
    for m in EMAIL_RE.findall(description or ""):
        if not _BAD_EMAIL.search(m):
            return m.lower()
    return None


def qualify(conn, cfg: dict, job_id: str, by: str = "manual", start: date | None = None) -> dict:
    """Mark a job qualified and generate its 3 drafts (idempotent)."""
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not job:
        raise KeyError(job_id)
    job = dict(job)
    lead = conn.execute("SELECT * FROM leads WHERE job_id = ?", (job_id,)).fetchone()
    now = _now()
    with conn:
        if lead is None:
            start = start or date.today()
            email = contact_from_posting(job.get("description")) if cfg["followups"].get("contact_from_posting") else None
            conn.execute(
                "INSERT INTO leads (job_id, qualified_at, qualified_by, contact_name, contact_email, contact_source, status, updated_at)"
                " VALUES (?,?,?,?,?,?, 'active', ?)",
                (job_id, start.isoformat(), by, None, email, "posting" if email else None, now),
            )
        elif lead["status"] == "closed":
            conn.execute("UPDATE leads SET status='active', updated_at=? WHERE job_id=?", (now, job_id))
    lead = dict(conn.execute("SELECT * FROM leads WHERE job_id = ?", (job_id,)).fetchone())
    _ensure_drafts(conn, cfg, job, lead)
    return lead


def _ensure_drafts(conn, cfg, job, lead):
    if conn.execute("SELECT COUNT(*) FROM followups WHERE job_id=?", (job["id"],)).fetchone()[0] >= 3:
        return
    seq = {e["touch"]: e for e in generate_sequence(job, lead, cfg)}
    dates = schedule(date.fromisoformat(lead["qualified_at"][:10]), cfg)
    now = _now()
    with conn:
        for touch, when in enumerate(dates, start=1):
            conn.execute(
                "INSERT INTO followups (id, job_id, touch, scheduled_for, status, subject, body, generator, created_at, updated_at)"
                " VALUES (?,?,?,?, 'draft', ?,?,?,?,?) ON CONFLICT (job_id, touch) DO NOTHING",
                (f"{job['id']}:{touch}", job["id"], touch, when.isoformat(),
                 seq[touch]["subject"], seq[touch]["body"], seq[touch]["generator"], now, now),
            )


def rerender_unedited(conn, cfg, job_id):
    """After a contact change, refresh drafts the user hasn't hand-edited (greeting uses the name).
    Model-written drafts keep their text and only get the new greeting line (no slow regeneration
    inside a web request); template drafts are re-rendered."""
    from .compose import greeting_for

    job = dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())
    lead = dict(conn.execute("SELECT * FROM leads WHERE job_id = ?", (job_id,)).fetchone())
    rows = conn.execute("SELECT id, touch, body, generator FROM followups WHERE job_id=? AND status='draft' AND edited=0",
                        (job_id,)).fetchall()
    if not rows:
        return
    seq = None
    greeting = greeting_for(job, lead, cfg)
    with conn:
        for f in rows:
            if (f["generator"] or "").startswith("ollama"):
                first, _, rest = f["body"].partition("\n")
                conn.execute("UPDATE followups SET body=?, updated_at=? WHERE id=?", (greeting + "\n" + rest, _now(), f["id"]))
                continue
            seq = seq or {e["touch"]: e for e in template_sequence(job, lead, cfg)}
            e = seq[f["touch"]]
            conn.execute("UPDATE followups SET subject=?, body=?, generator=?, updated_at=? WHERE id=?",
                         (e["subject"], e["body"], e["generator"], _now(), f["id"]))


def redraft(conn, cfg, job_id) -> int:
    """Rewrite this job's unedited, unsent drafts (e.g. after installing Ollama or changing the
    model). Dates and statuses are kept; hand-edited or approved drafts are never touched."""
    job = dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())
    lead = dict(conn.execute("SELECT * FROM leads WHERE job_id = ?", (job_id,)).fetchone())
    rows = conn.execute("SELECT id, touch FROM followups WHERE job_id=? AND status='draft' AND edited=0", (job_id,)).fetchall()
    if not rows:
        return 0
    seq = {e["touch"]: e for e in generate_sequence(job, lead, cfg)}
    with conn:
        for f in rows:
            e = seq[f["touch"]]
            conn.execute("UPDATE followups SET subject=?, body=?, generator=?, updated_at=? WHERE id=? AND status='draft' AND edited=0",
                         (e["subject"], e["body"], e["generator"], _now(), f["id"]))
    return len(rows)


def unqualify(conn, job_id):
    with conn:
        conn.execute("UPDATE followups SET status='skipped', updated_at=? WHERE job_id=? AND status IN ('draft','approved')", (_now(), job_id))
        conn.execute("UPDATE leads SET status='closed', updated_at=? WHERE job_id=?", (_now(), job_id))


def set_contact(conn, cfg, job_id, name, email):
    email = (email or "").strip() or None
    if email and not EMAIL_RE.fullmatch(email):
        raise ValueError(f"not an email address: {email}")
    with conn:
        conn.execute("UPDATE leads SET contact_name=?, contact_email=?, contact_source=?, updated_at=? WHERE job_id=?",
                     ((name or "").strip() or None, email, "manual" if email else None, _now(), job_id))
    rerender_unedited(conn, cfg, job_id)


def edit(conn, fid, subject, body):
    with conn:
        conn.execute("UPDATE followups SET subject=?, body=?, edited=1, updated_at=? WHERE id=? AND status IN ('draft','approved')",
                     (subject.strip(), body.strip(), _now(), fid))


def approve(conn, fid):
    """Approve = allowed to be sent by an explicit send-approved run. Requires a contact email."""
    row = conn.execute("SELECT l.contact_email, f.subject, f.body FROM followups f JOIN leads l ON l.job_id=f.job_id WHERE f.id=?", (fid,)).fetchone()
    if not row or not row["contact_email"]:
        raise ValueError("needs contact: add a recruiter/hiring email before approving")
    if MARKER_RE.search(row["subject"] + row["body"]):
        raise ValueError("draft still has [[EDIT ...]] placeholders; replace them (or fill in applicant: in config.yaml) before approving")
    with conn:
        conn.execute("UPDATE followups SET status='approved', approved_at=?, updated_at=? WHERE id=? AND status='draft'", (_now(), _now(), fid))


def unapprove(conn, fid):
    with conn:
        conn.execute("UPDATE followups SET status='draft', approved_at=NULL, updated_at=? WHERE id=? AND status='approved'", (_now(), fid))


def skip(conn, fid):
    with conn:
        conn.execute("UPDATE followups SET status='skipped', updated_at=? WHERE id=? AND status IN ('draft','approved')", (_now(), fid))


def mark_replied(conn, job_id):
    """Contact replied: stop all remaining touches for this job."""
    now = _now()
    with conn:
        conn.execute("UPDATE followups SET status='replied', updated_at=? WHERE job_id=? AND status IN ('draft','approved')", (now, job_id))
        conn.execute("UPDATE leads SET status='replied', replied_at=?, updated_at=? WHERE job_id=?", (now, now, job_id))


def auto_qualify(conn, cfg) -> int:
    thr = cfg["followups"].get("auto_qualify_score")
    if thr is None:
        return 0
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM jobs WHERE score >= ? AND id NOT IN (SELECT job_id FROM leads)", (float(thr),))]
    for jid in ids:
        qualify(conn, cfg, jid, by="auto")
    return len(ids)


def queue(conn, today: date | None = None) -> dict:
    today = (today or date.today()).isoformat()
    base = ("SELECT f.*, j.title, j.company, j.location, j.url, l.contact_name, l.contact_email, l.contact_source,"
            " l.status AS lead_status, l.qualified_at FROM followups f JOIN jobs j ON j.id=f.job_id JOIN leads l ON l.job_id=f.job_id ")
    q = lambda where, order, *p: [dict(r) for r in conn.execute(base + where + order, p)]
    return {
        "today": today,
        "due": q("WHERE f.status IN ('draft','approved') AND f.scheduled_for <= ? ", "ORDER BY f.scheduled_for, j.company, f.touch", today),
        "upcoming": q("WHERE f.status IN ('draft','approved') AND f.scheduled_for > ? ", "ORDER BY f.scheduled_for, j.company, f.touch LIMIT 200", today),
        "done": q("WHERE f.status IN ('sent','skipped','replied') ", "ORDER BY f.updated_at DESC LIMIT 100"),
    }


def qualified_ids(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT job_id FROM leads WHERE status <> 'closed'")}
