"""Email extraction/classification + contact safety for aggregator/enrich.py (offline, no network).

Run: python -m unittest discover -s tests -v   (or: python -m pytest tests)
"""
import sqlite3
import unittest

from aggregator import enrich


class ClassifyEmail(unittest.TestCase):
    def cls(self, email, ctx="", posting=False):
        return enrich.classify_email(email, ctx, from_posting=posting)[0]

    def test_known_findings(self):
        # real cases checked by hand
        self.assertEqual(self.cls("info@foreverext.com"), "GENERAL")      # FOREVER Exteriors
        self.assertEqual(self.cls("new_work@pcconstruction.com"), "IGNORE")  # PC Construction
        self.assertEqual(self.cls("pr@lessen.com"), "IGNORE")             # Lessen
        self.assertEqual(self.cls("info@roystonllc.com"), "GENERAL")      # Royston
        self.assertEqual(self.cls("hr@cmecorp.com"), "HIRING")
        self.assertEqual(self.cls("applynow@perimetersolutionsgroup.com"), "HIRING")
        self.assertEqual(self.cls("hrus@priceindustries.com"), "HIRING")

    def test_hiring_inboxes(self):
        for e in ("careers@acme.com", "jobs@acme.com", "recruiting@acme.com", "talent@acme.com", "talent.acquisition@acme.com",
                  "hr@acme.com", "humanresources@acme.com", "atl.careers@acme.com", "employment@acme.com"):
            self.assertEqual(self.cls(e), "HIRING", e)

    def test_general_never_upgraded(self):
        # a general inbox stays GENERAL even next to "send your resume" or inside a posting
        self.assertEqual(self.cls("info@acme.com", "Send your resume to", posting=True), "GENERAL")
        self.assertEqual(self.cls("contact@acme.com", posting=True), "GENERAL")
        self.assertEqual(self.cls("hello@acme.com"), "GENERAL")

    def test_ignore(self):
        for e in ("press@acme.com", "sales@acme.com", "support@acme.com", "privacy@acme.com", "legal@acme.com",
                  "accounting@acme.com", "noreply@acme.com", "hraccommodations@acme.com", "ada@acme.com", "billing@acme.com"):
            self.assertEqual(self.cls(e, "Send your resume to", posting=True), "IGNORE", e)

    def test_personal_address_needs_a_label(self):
        self.assertEqual(self.cls("jsmith@acme.com"), "GENERAL")                       # bare, on a company page
        self.assertEqual(self.cls("jsmith@acme.com", "Human Resources: Jane Smith"), "HIRING")  # labeled HR on the page
        self.assertEqual(self.cls("janesmith@staffingco.com", posting=True), "HIRING")      # written in the posting
        self.assertEqual(self.cls("jpi@acme.org", "If you need a reasonable accommodation when you apply, contact"), "IGNORE")
        self.assertEqual(self.cls("job.advert.accessibility@acme.com", posting=True), "IGNORE")
        self.assertEqual(self.cls("studentaffairs@tcsg.edu", "Career and Technical Education Student Affairs"), "GENERAL")


class Extract(unittest.TestCase):
    def test_find_emails_markdown_and_junk(self):
        text = r"Apply: new\_work@pc.com, **HR@Acme.com**. logo@2x.png sprite@icons.svg user@example.com"
        self.assertEqual([e for e, _ in enrich.find_emails(text)], ["new_work@pc.com", "hr@acme.com"])

    def test_posting_emails_cite_the_posting(self):
        got = enrich.posting_emails("Send resumes to careers@acme.com. Questions: info@acme.com", "jobs@acme.com",
                                    "https://example.org/job/1")
        by = {e["email"]: e for e in got}
        self.assertEqual(by["careers@acme.com"]["class"], "HIRING")
        self.assertEqual(by["info@acme.com"]["class"], "GENERAL")
        self.assertEqual(by["jobs@acme.com"]["class"], "HIRING")   # JobSpy `emails` field
        self.assertTrue(all(e["source_url"] == "https://example.org/job/1" for e in got))
        self.assertEqual(enrich.best_posting_contact("email info@acme.com or press@acme.com"), None)

    def test_parse_page_mailto_cloudflare_and_text(self):
        key = 0x42
        enc = "%02x" % key + "".join("%02x" % (ord(c) ^ key) for c in "jobs@acme.com")
        html = (f'<html><head><meta name="description" content="Acme builds roofs."></head><body>'
                f'<p>Careers: <a href="mailto:careers@acme.com?subject=hi">email us</a></p>'
                f'<span class="__cf_email__" data-cfemail="{enc}">[email protected]</span>'
                f'<footer>Call (770) 555-1234 or write info@acme.com</footer>'
                f'<a href="https://boards.greenhouse.io/acme">Jobs</a></body></html>')
        pg = enrich.parse_page("https://acme.com/", html)
        emails = {e["email"] for e in pg["emails"]}
        self.assertEqual(emails, {"careers@acme.com", "jobs@acme.com", "info@acme.com"})
        self.assertEqual(pg["description"], "Acme builds roofs.")
        self.assertEqual(enrich.fmt_phone(pg["phones"][0]), "(770) 555-1234")

    def test_page_label_is_local(self):
        html = ("<html><body><nav><a href='/careers'>Careers</a></nav><div><p>Student Affairs</p>"
                "<p>studentaffairs@tcsg.edu</p><p>Human Resources</p><p>jdoe@tcsg.edu</p></div></body></html>")
        pg = enrich.parse_page("https://tcsg.edu/contact", html)
        got = {e["email"]: enrich.classify_email(e["email"], e["context"])[0] for e in pg["emails"]}
        self.assertEqual(got, {"studentaffairs@tcsg.edu": "GENERAL", "jdoe@tcsg.edu": "HIRING"})

    def test_recruiter_name(self):
        self.assertEqual(enrich.recruiter_name("Questions? Contact Jane Doe at jane@acme.com"), "Jane Doe")
        self.assertEqual(enrich.recruiter_name("**Recruiter:** John Smith"), "John Smith")
        self.assertIsNone(enrich.recruiter_name("Contact: Human Resources"))


class Names(unittest.TestCase):
    def test_anonymous(self):
        self.assertTrue(enrich.is_anonymous("Multi Regional Roofing Company"))
        self.assertTrue(enrich.is_anonymous("Reputable Repairs and Maintanence Company"))
        self.assertTrue(enrich.is_anonymous("Confidential"))
        self.assertFalse(enrich.is_anonymous("PC Construction Company"))
        self.assertFalse(enrich.is_anonymous("Michael Page"))

    def test_domain_match(self):
        self.assertGreaterEqual(enrich.name_match_score("FOREVER Exteriors", "foreverext.com"), 2)
        self.assertGreaterEqual(enrich.name_match_score("Royston Plant", "roystonllc.com"), 2)
        self.assertGreaterEqual(enrich.name_match_score("DPR Construction", "dpr.com"), 2)
        self.assertLess(enrich.name_match_score("Price Industries Limited", "priceline.com"), 2)
        self.assertLess(enrich.name_match_score("FOREVER Exteriors", "foreverliving.com"), 2)
        self.assertLess(enrich.name_match_score("Technical College System of Georgia", "georgia.gov"), 2)
        self.assertGreaterEqual(enrich.name_match_score("Technical College System of Georgia", "tcsg.edu"), 2)

    def test_ats_board(self):
        self.assertEqual(enrich.ats_board_of("https://boards.greenhouse.io/embed/job_board?for=acme"), ("greenhouse", "acme"))
        self.assertEqual(enrich.ats_board_of("https://jobs.lever.co/lessen/123"), ("lever", "lessen"))
        self.assertEqual(enrich.ats_board_of("https://jobs.ashbyhq.com/lambda"), ("ashby", "lambda"))


class JobSpyFields(unittest.TestCase):
    def test_extras(self):
        from aggregator.sources.jobspy_source import extras

        nan = float("nan")
        x = extras({"emails": ["HR@cobbfendley.com", "hr@cobbfendley.com"], "job_url_direct": "https://grnh.se/abc",
                    "company_url_direct": nan, "company_addresses": "Phoenix, AZ", "company_rating": nan, "vacancy_count": 3.0,
                    "skills": ["a", "b"]})
        self.assertEqual(x["emails"], "hr@cobbfendley.com")
        self.assertEqual(x["job_url_direct"], "https://grnh.se/abc")
        self.assertIsNone(x["company_url_direct"])
        self.assertIsNone(x["company_rating"])
        self.assertEqual(x["vacancy_count"], 3)
        self.assertEqual(x["skills"], "a, b")


class ContactSafety(unittest.TestCase):
    """Only a HIRING address fills an EMPTY lead contact; a manual contact is never overwritten."""

    def setUp(self):
        from aggregator import db
        from aggregator.config import load_config

        self.cfg = load_config()
        self.cfg["database"]["path"] = ":memory:"
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)
        db._migrate(self.conn)  # noqa: SLF001
        now = "2026-10-07T12:00:00-04:00"
        for jid, desc in (("j1", "Send resumes to careers@acme.com"), ("j2", "Send resumes to careers@acme.com"),
                          ("j3", "Questions: info@acme.com")):
            self.conn.execute("INSERT INTO jobs (id, source, company, title, url, description, fetched_at, first_seen_at,"
                              " dedupe_hash, posted_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                              (jid, "indeed", "Acme Roofing", "Project Manager " + jid, f"https://x/{jid}", desc, now, now, jid, now))
        self.conn.execute("INSERT INTO leads VALUES ('j1', '2026-10-07', 'auto', NULL, NULL, NULL, 'active', NULL, ?)", (now,))
        self.conn.execute("INSERT INTO leads VALUES ('j2', '2026-10-07', 'manual', 'Pat', 'pat@acme.com', 'manual', 'active', NULL, ?)", (now,))
        self.conn.execute("INSERT INTO leads VALUES ('j3', '2026-10-07', 'auto', NULL, NULL, NULL, 'active', NULL, ?)", (now,))
        self.conn.commit()

    def test_fill_and_never_overwrite(self):
        self.cfg["enrich"].update(search=False, fetch_board_details=False)
        en = enrich.Enricher(self.conn, self.cfg, budget_s=0, max_companies=0)  # no web: cap reached immediately
        for jid in ("j1", "j2", "j3"):
            en.enrich_job(jid)
        lead = lambda j: dict(self.conn.execute("SELECT * FROM leads WHERE job_id=?", (j,)).fetchone())  # noqa: E731
        self.assertEqual(lead("j1")["contact_email"], "careers@acme.com")
        self.assertTrue(lead("j1")["contact_source"].startswith("enrich: https://x/j1"))
        self.assertEqual(lead("j2")["contact_email"], "pat@acme.com")       # manual contact untouched
        self.assertEqual(lead("j2")["contact_source"], "manual")
        self.assertIsNone(lead("j3")["contact_email"])                      # GENERAL info@ is never used
        self.assertEqual(enrich.brief(self.conn, "j3")["general_emails"][0]["email"], "info@acme.com")


if __name__ == "__main__":
    unittest.main()
