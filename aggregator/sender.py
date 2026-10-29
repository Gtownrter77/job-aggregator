"""SMTP sender for due follow-ups with a confirmed hiring contact.

Never imported by fetch/serve for side effects. Runs via:
  * `python -m aggregator send-due` / `send-approved` (explicit CLI)
  * `python -m aggregator auto` when followups.auto_send is true

Both paths refuse unless ALL of:
  * config followups.sending_enabled: true   (kill switch; default false)
  * env SMTP_HOST, SMTP_USER, SMTP_PASS, FROM_ADDR are set
  * the draft is due (scheduled_for <= today), lead active, contact email present
  * contact_source is a confirmed hiring source (posting | enrich:* | manual)
  * body/subject have no [[EDIT ...]] placeholders

Auto-send (auto_send: true) also sends status='draft' rows that pass the rails;
without auto_send, only status='approved' rows are sent (legacy Approve click).
Nothing invents recipients. Every attempt is logged.
"""
from __future__ import annotations

import logging
import os
import re
import smtplib
import ssl
from datetime import date, datetime
from email.message import EmailMessage
from pathlib import Path

from .compose import MARKER_RE
from .config import resolve

log = logging.getLogger("aggregator.sender")

REQUIRED_ENV = ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "FROM_ADDR")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_BAD_EMAIL = re.compile(
    r"(no-?reply|donotreply|example\.(com|org)|privacy|accommodation|ada@|eeo|\.(png|jpg|gif)$)",
    re.I,
)
# contact_source values that mean a human/published hiring inbox (never invented)
TRUSTED_CONTACT_SOURCES = frozenset({"posting", "manual"})


def contact_confirmed(source: str | None) -> bool:
    """True only for posting text, enrich-published HR, or a manually entered address."""
    s = (source or "").strip()
    return s in TRUSTED_CONTACT_SOURCES or s.startswith("enrich:")


def _log_send_line(cfg, line: str) -> None:
    try:
        path = resolve((cfg.get("auto") or {}).get("digest_dir") or "logs") / "sends.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line.rstrip() + "\n")
    except OSError as e:
        log.warning("could not write sends.log: %s", e)


def sendable(conn, cfg, today: date | None = None) -> list[dict]:
    """Due follow-ups that pass contact + draft rails (does not check kill switch / SMTP)."""
    today = (today or date.today()).isoformat()
    auto = bool((cfg.get("followups") or {}).get("auto_send"))
    statuses = ("draft", "approved") if auto else ("approved",)
    placeholders = ",".join("?" * len(statuses))
    rows = [dict(r) for r in conn.execute(
        "SELECT f.id, f.touch, f.subject, f.body, f.scheduled_for, f.status,"
        " l.contact_email, l.contact_source, j.company, j.title, j.id AS job_id"
        " FROM followups f JOIN leads l ON l.job_id=f.job_id JOIN jobs j ON j.id=f.job_id"
        f" WHERE f.status IN ({placeholders}) AND f.scheduled_for <= ? AND l.status='active'"
        " AND l.contact_email IS NOT NULL AND trim(l.contact_email) <> ''"
        " ORDER BY f.scheduled_for, f.touch", (*statuses, today))]
    out = []
    for it in rows:
        email = (it.get("contact_email") or "").strip()
        skip = None
        if not EMAIL_RE.fullmatch(email) or _BAD_EMAIL.search(email):
            skip = "invalid_or_bad_email"
        elif not contact_confirmed(it.get("contact_source")):
            skip = "unconfirmed_contact_source"
        elif MARKER_RE.search((it.get("subject") or "") + (it.get("body") or "")):
            skip = "has_placeholders"
        elif not (it.get("subject") or "").strip() or not (it.get("body") or "").strip():
            skip = "empty_subject_or_body"
        it["skip_reason"] = skip
        if skip is None:
            out.append(it)
        else:
            log.info("skip %s (%s -> %s): %s", it["id"], it.get("company"), email, skip)
    return out


def skipped_due(conn, cfg, today: date | None = None) -> list[dict]:
    """Due unsent rows that have an email but fail rails (for digests / dry-run)."""
    today = (today or date.today()).isoformat()
    auto = bool((cfg.get("followups") or {}).get("auto_send"))
    statuses = ("draft", "approved") if auto else ("approved",)
    placeholders = ",".join("?" * len(statuses))
    rows = [dict(r) for r in conn.execute(
        "SELECT f.id, f.touch, f.subject, f.body, f.scheduled_for, f.status,"
        " l.contact_email, l.contact_source, j.company, j.title"
        " FROM followups f JOIN leads l ON l.job_id=f.job_id JOIN jobs j ON j.id=f.job_id"
        f" WHERE f.status IN ({placeholders}) AND f.scheduled_for <= ? AND l.status='active'"
        " ORDER BY f.scheduled_for, f.touch", (*statuses, today))]
    skipped = []
    for it in rows:
        email = (it.get("contact_email") or "").strip()
        if not email:
            it["skip_reason"] = "no_email"
            skipped.append(it)
            continue
        if not EMAIL_RE.fullmatch(email) or _BAD_EMAIL.search(email):
            it["skip_reason"] = "invalid_or_bad_email"
        elif not contact_confirmed(it.get("contact_source")):
            it["skip_reason"] = "unconfirmed_contact_source"
        elif MARKER_RE.search((it.get("subject") or "") + (it.get("body") or "")):
            it["skip_reason"] = "has_placeholders"
        elif not (it.get("subject") or "").strip() or not (it.get("body") or "").strip():
            it["skip_reason"] = "empty_subject_or_body"
        else:
            continue
        skipped.append(it)
    return skipped


def preflight(cfg) -> list[str]:
    problems = []
    if not (cfg.get("followups") or {}).get("sending_enabled"):
        problems.append("followups.sending_enabled is false (kill switch off — set true in config.local.yaml to allow sends)")
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        problems.append("missing env vars: " + ", ".join(missing))
    return problems


def send_due(conn, cfg, dry_run: bool = False) -> dict:
    """Send every due follow-up that passes the rails (or list them on dry_run / blocked)."""
    items = sendable(conn, cfg)
    skipped = skipped_due(conn, cfg)
    # skipped_due includes no-email; exclude ones already in items
    skipped = [s for s in skipped if s["id"] not in {i["id"] for i in items}]
    problems = preflight(cfg)
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    if dry_run or problems:
        for it in items:
            _log_send_line(cfg, f"{stamp} DRY_RUN id={it['id']} to={it['contact_email']} "
                                f"company={it.get('company')} subject={it.get('subject')!r} "
                                f"blocked_by={problems or 'none'}")
        return {
            "sent": 0,
            "would_send": len(items),
            "items": items,
            "skipped": skipped,
            "blocked_by": problems,
            "dry_run": dry_run or bool(problems),
            "errors": [],
        }
    host, port = os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587"))
    sender = os.environ["FROM_ADDR"]
    sent, errors = 0, []
    ctx = ssl.create_default_context()
    smtp = smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) if port == 465 else smtplib.SMTP(host, port, timeout=30)
    try:
        if port != 465:
            smtp.starttls(context=ctx)
        smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        for it in items:
            # re-check status + contact right before sending (UI may have changed it)
            cur = conn.execute(
                "SELECT f.status, l.contact_email, l.contact_source, l.status AS lead_status"
                " FROM followups f JOIN leads l ON l.job_id=f.job_id WHERE f.id=?",
                (it["id"],),
            ).fetchone()
            if not cur or cur["lead_status"] != "active":
                continue
            auto = bool((cfg.get("followups") or {}).get("auto_send"))
            ok_status = cur["status"] in (("draft", "approved") if auto else ("approved",))
            if not ok_status:
                continue
            email = (cur["contact_email"] or "").strip()
            if not email or not contact_confirmed(cur["contact_source"]):
                continue
            msg = EmailMessage()
            msg["From"], msg["To"], msg["Subject"] = sender, email, it["subject"]
            msg.set_content(it["body"])
            try:
                smtp.send_message(msg)
                now = datetime.now().astimezone().isoformat(timespec="seconds")
                with conn:
                    conn.execute(
                        "UPDATE followups SET status='sent', sent_at=?, send_error=NULL, updated_at=? WHERE id=?",
                        (now, now, it["id"]),
                    )
                sent += 1
                line = (f"{now} SENT id={it['id']} to={email} company={it.get('company')} "
                        f"touch={it.get('touch')} subject={it.get('subject')!r} "
                        f"source={cur['contact_source']}")
                log.info(line)
                _log_send_line(cfg, line)
            except Exception as e:  # noqa: BLE001
                err = f"{it['id']}: {e}"
                errors.append(err)
                with conn:
                    conn.execute("UPDATE followups SET send_error=? WHERE id=?", (str(e)[:300], it["id"]))
                _log_send_line(cfg, f"{stamp} ERROR id={it['id']} to={email} err={e}")
                log.exception("send failed for %s", it["id"])
    finally:
        try:
            smtp.quit()
        except Exception:
            pass
    return {
        "sent": sent,
        "would_send": len(items),
        "errors": errors,
        "items": items,
        "skipped": skipped,
        "blocked_by": [],
        "dry_run": False,
    }


# Back-compat alias used by older CLI / docs
send_approved = send_due
