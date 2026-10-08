"""Phone snapshot (aggregator/snapshot.py): escaping, mailto encoding, lead ordering. Offline, temp DB.

Run: python -m pytest tests/test_snapshot.py
"""
import copy
import html
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote

from aggregator import db, snapshot
from aggregator.config import DEFAULTS

NOW = datetime(2026, 10, 8, 14, 35, tzinfo=timezone.utc)  # 10:35 AM ET
EVIL = '<script>alert("x")</script><img src=x onerror=alert(1)>'


def _job(conn, jid, title, score, track="atlanta", url="https://example.org/j", desc="", company="Acme Roofing"):
    conn.execute(
        "INSERT INTO jobs (id, source, company, title, location, url, description, posted_at, fetched_at, first_seen_at,"
        " score, dedupe_hash, track) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (jid, "indeed", company, title, "Atlanta, GA", url, desc, "2026-10-07", NOW.isoformat(), "2026-10-08T12:00:00+00:00",
         score, "h" + jid, track))


def _lead(conn, jid, email, dates, status="draft"):
    conn.execute("INSERT INTO leads (job_id, qualified_at, qualified_by, contact_email, status, updated_at)"
                 " VALUES (?, '2026-10-01', 'manual', ?, 'active', 'x')", (jid, email))
    for touch, d in enumerate(dates, 1):
        conn.execute("INSERT INTO followups (id, job_id, touch, scheduled_for, status, subject, body, created_at, updated_at)"
                     " VALUES (?,?,?,?,?,?,?, 'x', 'x')",
                     (f"{jid}:{touch}", jid, touch, d, status, f"Re: role & next steps {touch}",
                      f"Hi team,\n\nLine two & 100% sure? a=b#c {EVIL}\n\nRyan"))


class Snapshot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = copy.deepcopy(DEFAULTS)
        self.cfg["database"]["path"] = str(Path(self.tmp.name) / "t.db")
        conn = db.connect(self.cfg)
        with conn:
            _job(conn, "a" * 40, "Upcoming high score", 0.9)
            _job(conn, "b" * 40, f"Overdue {EVIL}", 0.1, url="javascript:alert(1)", desc=f"<p>Great job</p>{EVIL} " + "word " * 200)
            _job(conn, "c" * 40, "Remote AI new job", 0.5, track="remote_ai")
            _job(conn, "d" * 40, "Atlanta new job", 0.4)
            _lead(conn, "a" * 40, "jobs@acme.com", ["2026-10-20", "2026-10-23", "2026-10-30"])
            _lead(conn, "b" * 40, None, ["2026-10-06", "2026-10-09", "2026-10-16"])
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def build(self):
        out = Path(self.tmp.name) / "snap.html"
        path, data = snapshot.build(self.cfg, out=out, now=NOW)
        return path.read_text(), data

    def test_counts_order_and_header(self):
        page, data = self.build()
        self.assertEqual(data["generated"], "Oct 8, 2026 10:35 AM ET")
        self.assertEqual(data["counts"]["leads"], 2)
        self.assertEqual(data["counts"]["followups_due"], 1)
        # overdue lead first even though its score is lower
        self.assertTrue(data["leads"][0]["title"].startswith("Overdue"))
        self.assertEqual([j["title"] for j in data["new_jobs"]["remote_ai"]], ["Remote AI new job"])
        self.assertEqual([j["title"] for j in data["new_jobs"]["atlanta"]], ["Atlanta new job"])  # leads excluded
        self.assertIn('name="robots" content="noindex', page)
        self.assertIn("Snapshot from Oct 8, 2026 10:35 AM ET", page)
        self.assertNotIn("updates arrive by email", page)
        self.assertIn(snapshot.MAILTO_NOTE, page)

    def test_escaping_and_no_unsafe_links(self):
        page, _ = self.build()
        self.assertEqual(len(re.findall(r"<script", page, re.I)), 0)
        self.assertNotIn("<img", page)
        self.assertNotIn("javascript:", page)
        self.assertIn("&lt;script&gt;", page)

    def test_mailto_roundtrip(self):
        page, data = self.build()
        links = [html.unescape(h) for h in re.findall(r'href="(mailto:[^"]*)"', page)]
        self.assertEqual(len(links), 6)
        with_to = [h for h in links if h.startswith("mailto:jobs@acme.com?")]
        no_to = [h for h in links if h.startswith("mailto:?")]
        self.assertEqual((len(with_to), len(no_to)), (3, 3))
        q = parse_qs(with_to[0].split("?", 1)[1], keep_blank_values=True)
        self.assertEqual(q["subject"], ["Re: role & next steps 1"])
        self.assertEqual(q["body"][0].replace("\r\n", "\n"), f"Hi team,\n\nLine two & 100% sure? a=b#c {EVIL}\n\nRyan")
        self.assertNotIn(" ", with_to[0])
        self.assertNotIn("\n", with_to[0])

    def test_helpers(self):
        self.assertEqual(snapshot.safe_url("javascript:alert(1)"), "")
        self.assertEqual(snapshot.safe_url("https://a.com/x?y=1"), "https://a.com/x?y=1")
        self.assertEqual(unquote(snapshot.mailto("not an email", "s", "b").split("?")[0]), "mailto:")
        ex = snapshot.excerpt("<p>Hello <b>world</b></p> **bold** " + "x " * 400)
        self.assertLessEqual(len(ex), 301)
        self.assertTrue(ex.startswith("Hello world bold"))

    def test_publish_refuses_main(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["dashboard"]["branch"] = "main"
        res = snapshot.publish(cfg, Path(self.tmp.name) / "nope.html")
        self.assertFalse(res["ok"])


if __name__ == "__main__":
    unittest.main()
