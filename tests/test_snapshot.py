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
        # exactly one script: the inline filter code (no src); user data never becomes markup
        self.assertEqual(len(re.findall(r"<script", page, re.I)), 1)
        self.assertEqual(len(re.findall(r"<script>", page)), 1)
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

    def test_categories_and_county(self):
        self.assertEqual(snapshot.categories("Roofing Sales Representative"), ["roofing", "sales"])
        self.assertEqual(snapshot.categories("Construction Project Manager"), ["construction"])
        self.assertEqual(snapshot.categories("Applied AI Engineer"), ["engineering", "ai"])
        self.assertEqual(snapshot.categories("HVAC Technician"), ["trades"])
        self.assertEqual(snapshot.categories("Cashier"), ["other"])
        self.assertEqual(snapshot.categories("Shanghai Retail Associate"), ["other"])
        self.assertEqual(snapshot._county("Marietta, GA, US", {}), "Cobb County")
        self.assertEqual(snapshot._county("Remote", {}), "")

    def test_filter_data_attributes(self):
        page, data = self.build()
        self.assertIn('id="f-panel"', page)
        self.assertIn('data-act="dismiss"', page)
        for fid in ("f-q", "f-region", "f-cat", "f-em", "f-fu", "f-sc", "f-po", "f-place", "f-hide", "f-sort", "f-reset", "f-count"):
            self.assertIn(f'id="{fid}"', page)
        cards = dict(re.findall(r'data-id="([0-9a-f]{40})"[^>]*?data-fu="(\w+)"', page))
        self.assertEqual(cards["b" * 40], "overdue")      # touch 1 on Oct 6, today Oct 8
        self.assertEqual(cards["a" * 40], "notdue")       # first touch Oct 20
        self.assertEqual(cards["c" * 40], "none")         # not a lead
        self.assertIn('data-due="2026-10-20"', page)
        self.assertIn('data-tr="remote_ai"', page)
        self.assertIn('data-place="atlanta, ga fulton county"', page)
        self.assertIn('data-po="2026-10-07"', page)
        self.assertIn('data-em="1"', page)               # lead a has a contact email
        self.assertIn('<body data-today="2026-10-08">', page)

    def test_publish_refuses_main(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["dashboard"]["branch"] = "main"
        res = snapshot.publish(cfg, Path(self.tmp.name) / "nope.html")
        self.assertFalse(res["ok"])


    def test_send_ready_approve_and_applied_warn(self):
        page, data = self.build()
        self.assertIn("✉ Send", page)  # prominent Send label in drafts
        self.assertIn('id="f-ready"', page)
        self.assertIn('value="ready"', page)
        self.assertIn("Ready to send", page)
        self.assertIn("Approve for Grok Bot", page)
        self.assertIn('id="abar"', page)
        self.assertIn("Copy list for chat", page)
        self.assertIn("jobdash.approve.v1", page)
        # fixture bodies include EVIL but not "I just applied" — no warn expected
        self.assertNotIn("Says you applied", page)
        # lead with email has has_email; overdue lead has no email
        self.assertTrue(any(L["has_email"] for L in data["leads"]))
        # touches carry warn_applied / short_id
        for L in data["leads"]:
            for t in L["touches"]:
                self.assertIn("warn_applied", t)
                self.assertIn("short_id", t)
                self.assertIn("claims_applied", t)

    def test_applied_claim_autofilled_for_send(self):
        """False 'I just applied' is rewritten so one-tap Send has no blanks / no warn."""
        conn = db.connect(self.cfg)
        with conn:
            jid = "e" * 40
            _job(conn, jid, "Applied claim job", 0.8)
            conn.execute("INSERT INTO leads (job_id, qualified_at, qualified_by, contact_email, status, updated_at)"
                         " VALUES (?, '2026-10-01', 'manual', 'hire@co.com', 'active', 'x')", (jid,))
            conn.execute(
                "INSERT INTO followups (id, job_id, touch, scheduled_for, status, subject, body, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?, 'x', 'x')",
                (f"{jid}:1", jid, 1, "2026-10-08", "draft", "Hello",
                 "Hi team,\n\nI just applied for the role and wanted to say hello.\n\nRyan"))
        conn.close()
        page, data = self.build()
        lead = next(L for L in data["leads"] if L["id"] == jid)
        t0 = lead["touches"][0]
        self.assertFalse(lead["applied"])
        self.assertIn("I'm applying for the role", t0["body"])
        self.assertNotIn("I just applied", t0["body"])
        self.assertFalse(t0["claims_applied"])
        self.assertFalse(t0["warn_applied"])
        self.assertFalse(t0["needs_detail"])
        self.assertTrue(t0["mailto"].startswith("mailto:hire@co.com?"))
        self.assertIn("I%27m%20applying%20for%20the%20role", t0["mailto"])
        self.assertIn("✉ Send email · touch 1", page)
        self.assertNotIn("Says you applied", page)
        self.assertNotIn("Needs a detail", page)

    def test_needs_detail_hides_send(self):
        conn = db.connect(self.cfg)
        with conn:
            jid = "f" * 40
            _job(conn, jid, "Blank draft job", 0.7)
            conn.execute("INSERT INTO leads (job_id, qualified_at, qualified_by, contact_email, status, updated_at)"
                         " VALUES (?, '2026-10-01', 'manual', 'hire@co.com', 'active', 'x')", (jid,))
            # Unfillable leftover after autofill: unknown bracket token we don't map
            conn.execute(
                "INSERT INTO followups (id, job_id, touch, scheduled_for, status, subject, body, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?, 'x', 'x')",
                (f"{jid}:1", jid, 1, "2026-10-08", "draft", "Hello",
                 "Hi team,\n\nPlease see [UnknownField] for details.\n\nRyan"))
        conn.close()
        page, data = self.build()
        lead = next(L for L in data["leads"] if L["id"] == jid)
        t0 = lead["touches"][0]
        self.assertTrue(t0["needs_detail"])
        self.assertEqual(t0["mailto"], "")
        self.assertIn("Needs a detail", page)



if __name__ == "__main__":
    unittest.main()
