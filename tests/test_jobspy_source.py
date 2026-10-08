import unittest

from aggregator.config import DEFAULTS
from aggregator.sources import jobspy_source as js


def _cfg():
    import copy
    cfg = copy.deepcopy(DEFAULTS)
    cfg["search"].update(location="Atlanta, GA", distance_miles=50, hours_old=168, roles=[])
    return cfg


class JobSpyOptions(unittest.TestCase):
    def test_per_site(self):
        self.assertEqual(js.per_site({"indeed": 100, "default": 40}, "indeed"), 100)
        self.assertEqual(js.per_site({"indeed": 100, "default": 40}, "linkedin"), 40)
        self.assertEqual(js.per_site(25, "linkedin"), 25)
        self.assertEqual(js.per_site(None, "linkedin", 7), 7)

    def test_google_needs_natural_language_query(self):
        cfg = _cfg()
        plan = js.default_plan(cfg)
        kw = js.scrape_kwargs("google", "project manager", {**plan, "hours_old": 24}, cfg["jobspy"])
        self.assertEqual(kw["google_search_term"], "project manager jobs near Atlanta, GA since yesterday")
        kw = js.scrape_kwargs("google", "AI trainer", {**plan, "is_remote": True}, cfg["jobspy"])
        self.assertEqual(kw["google_search_term"], "AI trainer jobs remote in the last week")

    def test_every_scrape_jobs_option_is_passed(self):
        cfg = _cfg()
        cfg["jobspy"].update(linkedin_company_ids=[1441], user_agent="UA", proxies=["localhost"], ca_cert="/x.pem")
        plan = js.default_plan(cfg)
        kw = js.scrape_kwargs("linkedin", "nurse", plan, cfg["jobspy"])
        for k in ("site_name", "search_term", "location", "distance", "is_remote", "job_type", "easy_apply",
                  "results_wanted", "hours_old", "country_indeed", "offset", "fetch_description",
                  "enforce_annual_salary", "description_format", "verbose", "linkedin_company_ids",
                  "proxies", "ca_cert", "user_agent"):
            self.assertIn(k, kw)
        self.assertEqual(kw["results_wanted"], 40)
        self.assertEqual(js.scrape_kwargs("indeed", "nurse", plan, cfg["jobspy"])["results_wanted"], 100)
        self.assertNotIn("linkedin_company_ids", js.scrape_kwargs("indeed", "nurse", plan, cfg["jobspy"]))

    def test_detail_modes(self):
        cfg = _cfg()
        plan = js.default_plan(cfg)
        j = cfg["jobspy"]
        self.assertEqual(js.detail_mode("linkedin", plan, j), "new")
        self.assertEqual(js.detail_mode("indeed", plan, j), "off")  # Indeed results already carry everything
        self.assertEqual(js.detail_mode("linkedin", plan, {**j, "fetch_description": True}), "search")

    def test_all_jobspy_output_columns_kept(self):
        from jobspy.util import desired_order
        from aggregator import db
        row = {c: None for c in desired_order}
        job = js._row_to_job("indeed", {**row, "title": "x", "job_url": "u"})
        core = {"id", "site", "job_url", "title", "company", "location", "date_posted", "job_type", "interval",
                "min_amount", "max_amount", "currency", "is_remote", "description"}
        for c in set(desired_order) - core:
            self.assertIn(c, db.JOBSPY_COLS, c)
            self.assertIn(c, job, c)


if __name__ == "__main__":
    unittest.main()
