"""SMTP sender for APPROVED follow-ups only.

Never imported by fetch/serve. Runs only via an explicit
`python -m aggregator send-approved`, and only when BOTH:
  * config followups.sending_enabled: true
  * env SMTP_HOST, SMTP_USER, SMTP_PASS, FROM_ADDR are set (SMTP_PORT optional, default 587;
    465 = implicit TLS, anything else = STARTTLS)
Only rows with status='approved', scheduled_for <= today, an active lead and a
contact email are sent. Each successful send is marked 'sent' immediately.
"""
from __future__ import annotations

import os
import smtplib
import ssl
from datetime import date, datetime
from email.message import EmailMessage

REQUIRED_ENV = ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "FROM_ADDR")


def sendable(conn, today: date | None = None) -> list[dict]:
    today = (today or date.today()).isoformat()
    return [dict(r) for r in conn.execute(
        "SELECT f.id, f.touch, f.subject, f.body, f.scheduled_for, l.contact_email, j.company, j.title"
        " FROM followups f JOIN leads l ON l.job_id=f.job_id JOIN jobs j ON j.id=f.job_id"
        " WHERE f.status='approved' AND f.scheduled_for <= ? AND l.status='active'"
        " AND l.contact_email IS NOT NULL AND l.contact_email <> ''"
        " ORDER BY f.scheduled_for, f.touch", (today,))]


def preflight(cfg) -> list[str]:
    problems = []
    if not cfg["followups"].get("sending_enabled"):
        problems.append("followups.sending_enabled is false in config.yaml")
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        problems.append("missing env vars: " + ", ".join(missing))
    return problems


def send_approved(conn, cfg, dry_run: bool = False) -> dict:
    items = sendable(conn)
    problems = preflight(cfg)
    if dry_run or problems:
        return {"sent": 0, "would_send": len(items), "items": items, "blocked_by": problems, "dry_run": dry_run}
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
            # re-check status right before sending (UI may have changed it)
            cur = conn.execute("SELECT status FROM followups WHERE id=?", (it["id"],)).fetchone()
            if not cur or cur["status"] != "approved":
                continue
            msg = EmailMessage()
            msg["From"], msg["To"], msg["Subject"] = sender, it["contact_email"], it["subject"]
            msg.set_content(it["body"])
            try:
                smtp.send_message(msg)
                with conn:
                    conn.execute("UPDATE followups SET status='sent', sent_at=?, send_error=NULL, updated_at=? WHERE id=?",
                                 (datetime.now().astimezone().isoformat(timespec="seconds"),) * 2 + (it["id"],))
                sent += 1
            except Exception as e:  # noqa: BLE001
                errors.append(f"{it['id']}: {e}")
                with conn:
                    conn.execute("UPDATE followups SET send_error=? WHERE id=?", (str(e)[:300], it["id"]))
    finally:
        try:
            smtp.quit()
        except Exception:
            pass
    return {"sent": sent, "errors": errors, "items": items, "blocked_by": [], "dry_run": False}
