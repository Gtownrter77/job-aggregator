"""Auto-send rails: confirmed hiring contacts only; kill switch; no inventing recipients."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from aggregator import db, sender


def _cfg(tmp: Path, **fu) -> dict:
    followups = {
        "sending_enabled": False,
        "auto_send": True,
        "schedule_days": [0, 3, 10],
        "skip_weekends": False,
        "contact_from_posting": True,
        "applicant_name": "Ryan",
        "applicant_phone": "",
        "applicant_email": "",
        "default_contact_name": "Hiring Team",
        "templates_file": "followup_templates.yaml",
        "use_llm": False,
    }
    followups.update(fu)
    return {
        "database": {"path": str(tmp / "t.db")},
        "followups": followups,
        "auto": {"digest_dir": str(tmp / "logs")},
        "applicant": {"headline": "", "skills": [], "accomplishments": [], "profile_file": ""},
        "llm": {"enabled": False},
    }


def _seed(conn, *, job_id="j1", email="careers@acme.com", source="enrich: https://acme.com/careers",
          status="draft", when=None, subject="Hello", body="Body text here.", lead_status="active"):
    when = (when or date.today()).isoformat()
    now = "2026-10-28T12:00:00-04:00"
    with conn:
        conn.execute(
            "INSERT INTO jobs (id, source, company, title, location, url, description, posted_at,"
            " fetched_at, first_seen_at, score, dedupe_hash, track)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, "indeed", "Acme", "PM", "Atlanta, GA", "https://x/" + job_id, "desc",
             now, now, now, 0.1, "h" + job_id, "atlanta"),
        )
        conn.execute(
            "INSERT INTO leads (job_id, qualified_at, qualified_by, contact_name, contact_email, contact_source, status, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (job_id, when, "manual", None, email, source, lead_status, now),
        )
        conn.execute(
            "INSERT INTO followups (id, job_id, touch, scheduled_for, status, subject, body, generator, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"{job_id}:1", job_id, 1, when, status, subject, body, "template", now, now),
        )


class ContactConfirmed(unittest.TestCase):
    def test_trusted_sources(self):
        self.assertTrue(sender.contact_confirmed("posting"))
        self.assertTrue(sender.contact_confirmed("manual"))
        self.assertTrue(sender.contact_confirmed("enrich: https://acme.com/hr"))
        self.assertFalse(sender.contact_confirmed(None))
        self.assertFalse(sender.contact_confirmed(""))
        self.assertFalse(sender.contact_confirmed("guessed"))
        self.assertFalse(sender.contact_confirmed("linkedin"))


class SendableRails(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = _cfg(Path(self.tmp.name))
        self.conn = db.connect(self.cfg)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_includes_due_draft_with_enrich_source(self):
        _seed(self.conn)
        items = sender.sendable(self.conn, self.cfg)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["contact_email"], "careers@acme.com")

    def test_skips_no_email(self):
        _seed(self.conn, email=None, source=None)
        # rewrite lead with null email
        self.conn.execute("UPDATE leads SET contact_email=NULL, contact_source=NULL")
        self.conn.commit()
        self.assertEqual(sender.sendable(self.conn, self.cfg), [])
        skipped = sender.skipped_due(self.conn, self.cfg)
        self.assertTrue(any(s["skip_reason"] == "no_email" for s in skipped))

    def test_skips_unconfirmed_source(self):
        _seed(self.conn, source="scraped-somewhere")
        self.assertEqual(sender.sendable(self.conn, self.cfg), [])
        skipped = sender.skipped_due(self.conn, self.cfg)
        self.assertEqual(skipped[0]["skip_reason"], "unconfirmed_contact_source")

    def test_skips_placeholders(self):
        _seed(self.conn, body="Please [[EDIT this line]] thanks")
        self.assertEqual(sender.sendable(self.conn, self.cfg), [])
        self.assertEqual(sender.skipped_due(self.conn, self.cfg)[0]["skip_reason"], "has_placeholders")

    def test_skips_bad_email(self):
        _seed(self.conn, email="noreply@acme.com", source="posting")
        self.assertEqual(sender.sendable(self.conn, self.cfg), [])
        self.assertEqual(sender.skipped_due(self.conn, self.cfg)[0]["skip_reason"], "invalid_or_bad_email")

    def test_future_not_due(self):
        _seed(self.conn, when=date.today() + timedelta(days=5))
        self.assertEqual(sender.sendable(self.conn, self.cfg), [])

    def test_auto_send_off_requires_approved(self):
        self.cfg["followups"]["auto_send"] = False
        _seed(self.conn, status="draft")
        self.assertEqual(sender.sendable(self.conn, self.cfg), [])
        self.conn.execute("UPDATE followups SET status='approved'")
        self.conn.commit()
        self.assertEqual(len(sender.sendable(self.conn, self.cfg)), 1)

    def test_kill_switch_blocks_send(self):
        _seed(self.conn)
        res = sender.send_due(self.conn, self.cfg, dry_run=False)
        self.assertEqual(res["sent"], 0)
        self.assertTrue(any("sending_enabled" in p for p in res["blocked_by"]))
        self.assertEqual(res["would_send"], 1)

    def test_dry_run_never_marks_sent(self):
        _seed(self.conn)
        self.cfg["followups"]["sending_enabled"] = True
        with mock.patch.dict(os.environ, {
            "SMTP_HOST": "smtp.example", "SMTP_USER": "u", "SMTP_PASS": "p", "FROM_ADDR": "me@x.com",
        }):
            res = sender.send_due(self.conn, self.cfg, dry_run=True)
        self.assertEqual(res["sent"], 0)
        self.assertEqual(res["would_send"], 1)
        row = self.conn.execute("SELECT status FROM followups").fetchone()
        self.assertEqual(row["status"], "draft")
        log = Path(self.tmp.name) / "logs" / "sends.log"
        self.assertTrue(log.exists())
        self.assertIn("DRY_RUN", log.read_text())

    def test_manual_and_posting_sources_ok(self):
        for i, src in enumerate(("manual", "posting")):
            jid = f"j{i+2}"
            _seed(self.conn, job_id=jid, email=f"hr{i}@co.com", source=src)
        items = sender.sendable(self.conn, self.cfg)
        # j1 from setUp wasn't seeded in this test — only the two above if setUp empty... setUp has empty db
        # wait, this test class setUp creates empty db; we seeded two
        self.assertEqual({i["contact_source"] for i in items}, {"manual", "posting"})


if __name__ == "__main__":
    unittest.main()
