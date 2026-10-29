"""prune_stale closes old no-email leads; keeps emailed / high-score ones."""
import copy
import tempfile
import unittest
from pathlib import Path

from aggregator import db, followups
from aggregator.config import DEFAULTS


class PruneStale(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = copy.deepcopy(DEFAULTS)
        self.cfg["database"]["path"] = str(Path(self.tmp.name) / "t.db")
        self.conn = db.connect(self.cfg)
        with self.conn:
            for jid, company, score, qat, email in [
                ("1" * 40, "Old NoEmail", 0.2, "2026-10-10", None),
                ("2" * 40, "Old WithEmail", 0.2, "2026-10-10", "hr@co.com"),
                ("3" * 40, "Old HighScore", 0.85, "2026-10-10", None),
                ("4" * 40, "Fresh NoEmail", 0.2, "2026-10-25", None),
            ]:
                self.conn.execute(
                    "INSERT INTO jobs (id, source, company, title, location, url, description, posted_at,"
                    " fetched_at, first_seen_at, score, dedupe_hash, track)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (jid, "indeed", company, "PM", "Atlanta, GA", "https://x/" + jid, "",
                     "2026-10-01", "2026-10-01", "2026-10-01", score, "h" + jid, "atlanta"))
                self.conn.execute(
                    "INSERT INTO leads (job_id, qualified_at, qualified_by, contact_email, status, updated_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (jid, qat, "manual", email, "active", "x"))
                self.conn.execute(
                    "INSERT INTO followups (id, job_id, touch, scheduled_for, status, subject, body, created_at, updated_at)"
                    " VALUES (?,?,1,?,?,?,?,?,?)",
                    (jid + ":1", jid, "2026-10-12", "draft", "Hi", "Body", "x", "x"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_dry_run_and_prune(self):
        dry = followups.prune_stale(self.conn, before="2026-10-19", require_no_email=True,
                                    keep_min_score=0.7, dry_run=True)
        ids = {r["job_id"] for r in dry["jobs"]}
        self.assertEqual(ids, {"1" * 40})  # only old + no email + low score
        res = followups.prune_stale(self.conn, before="2026-10-19", require_no_email=True,
                                    keep_min_score=0.7, dry_run=False)
        self.assertEqual(res["closed"], 1)
        self.assertGreaterEqual(res["drafts_skipped"], 1)
        active = {r[0] for r in self.conn.execute("SELECT job_id FROM leads WHERE status='active'")}
        self.assertEqual(active, {"2" * 40, "3" * 40, "4" * 40})


if __name__ == "__main__":
    unittest.main()
