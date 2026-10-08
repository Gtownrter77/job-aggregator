"""JobSpy (github.com/speedyapply/JobSpy, python-jobspy 1.2.x) wrapper.

Supported boards: indeed, linkedin, zip_recruiter, glassdoor, google, bayt, naukri, bdjobs
(bayt = Middle East, naukri = India, bdjobs = Bangladesh: off by default for an Atlanta /
US-remote search). Each site runs in its own thread, one search term at a time, fully
isolated: a failing site is logged (with JobSpy's own error message) and skipped, never
aborting the rest of the fetch.

Every scrape_jobs() option is configurable under `jobspy:` (see config.yaml). Full postings
(description, emails, employer website/HQ/size/industry) are fetched per site either by JobSpy
at search time (`details: search`) or - cheaper - only for postings the database doesn't
already have a description for (`details: new`), using JobSpy's own per-site detail request.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from ..normalize import is_remote_text, num, to_iso

log = logging.getLogger("aggregator.jobspy")

SITES = ["indeed", "linkedin", "google", "zip_recruiter", "glassdoor", "bayt", "naukri", "bdjobs"]
_LOGGER_NAMES = {
    "indeed": "JobSpy:Indeed", "linkedin": "JobSpy:LinkedIn", "google": "JobSpy:Google",
    "zip_recruiter": "JobSpy:ZipRecruiter", "glassdoor": "JobSpy:Glassdoor",
    "bayt": "JobSpy:Bayt", "naukri": "JobSpy:Naukri", "bdjobs": "JobSpy:BDJobs",
}


class _Capture(logging.Handler):
    """Collects JobSpy's internal ERROR logs so we can report *why* a site failed."""

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.msgs: dict[str, list[str]] = {}
        self._lock = threading.Lock()

    def emit(self, record):
        with self._lock:
            self.msgs.setdefault(record.name, []).append(record.getMessage())

    def take(self, name: str) -> list[str]:
        with self._lock:
            return self.msgs.pop(name, [])


_capture = _Capture()
for _n in _LOGGER_NAMES.values():
    logging.getLogger(_n).addHandler(_capture)


def _str(v) -> str | None:
    """JobSpy/pandas value -> clean str (NaN/None/'' -> None; lists -> comma list)."""
    if v is None:
        return None
    if isinstance(v, (list, tuple, set)):
        items = [str(x).strip() for x in v if x is not None and str(x).strip()]
        return ", ".join(dict.fromkeys(items)) or None
    if isinstance(v, float):
        return None if v != v else str(v)
    s = str(v).strip()
    return s if s and s.lower() not in ("nan", "none", "n/a") else None


def _float(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _int(v) -> int | None:
    f = _float(v)
    return None if f is None else int(f)


def per_site(v, site: str, default=None):
    """Config value that may be per-site: {indeed: 100, default: 40} or a plain value."""
    if isinstance(v, dict):
        return v.get(site, v.get("default", default))
    return default if v is None else v


def detail_mode(site: str, plan: dict, j: dict) -> str:
    """How full postings are fetched for this site: 'search' (JobSpy fetch_description=True:
    every result, at search time), 'new' (only results not yet described in the DB), or 'off'."""
    if j.get("fetch_description") or (site == "linkedin" and j.get("linkedin_fetch_description")):
        return "search"
    for src in (plan.get("details"), j.get("details")):
        m = per_site(src, site)
        if m:
            return {True: "search", False: "off"}.get(m, str(m))
    sites = plan.get("fetch_description_sites")
    if sites is None:
        sites = j.get("fetch_description_sites") or []
    return "search" if site in sites else "off"


def detail_sites(site: str, plan: dict, j: dict) -> bool:
    """Back-compat: True when JobSpy itself fetches every posting at search time."""
    return detail_mode(site, plan, j) == "search"


class _Details:
    """Fetch the full posting for results the DB has no description for, with JobSpy's own
    per-site detail request (the one fetch_description=True makes). Capped per run; stops a
    site after 5 empty answers in a row (rate-limited / blocked)."""

    def __init__(self, site: str, j: dict, known: set | None, lock: threading.Lock, spent: dict | None = None):
        self.site = site
        # caps are per fetch run, shared by the tracks (spent = {site: {"n": .., "t": ..}})
        self.spent = spent if spent is not None else {}
        prev = self.spent.get(site) or {}
        self.known = known if known is not None else set()
        self.lock = lock
        self.max_n = int(per_site(j.get("detail_max_per_run"), site, 300))
        self.max_s = float(per_site(j.get("detail_max_seconds"), site, 300))
        self.delay = float(per_site(j.get("detail_delay_seconds"), site, 0.3))
        self.n = self.ok = self.empty_streak = 0
        self.t = 0.0
        self.n_prev, self.t_prev = int(prev.get("n", 0)), float(prev.get("t", 0.0))
        self.stopped = ""
        self._scraper = None
        self.fmt = j.get("description_format") or "markdown"

    def _get(self):
        if self._scraper is not None:
            return self._scraper
        from jobspy.model import DescriptionFormat, ScraperInput, Site
        from jobspy.util import create_session

        si = ScraperInput(site_type=[Site(self.site)], description_format=DescriptionFormat(self.fmt))
        if self.site == "linkedin":
            from jobspy.linkedin import LinkedIn
            sc = LinkedIn()
        elif self.site == "zip_recruiter":
            from jobspy.ziprecruiter import ZipRecruiter
            sc = ZipRecruiter()
            sc.session = create_session()
            sc.session.impersonate = "safari"
        elif self.site == "glassdoor":
            from jobspy.glassdoor import Glassdoor
            from jobspy.glassdoor.constant import headers
            from jobspy.model import Country
            sc = Glassdoor()
            sc.base_url = Country.USA.get_glassdoor_url()
            sc.session = create_session()
            sc.session.headers.update(headers)
            sc.session.get(sc._autocomplete_url("remote"), timeout=15)  # noqa: SLF001 - sets the cookies the API needs
        else:
            raise ValueError(f"no detail fetch for {self.site}")
        sc.scraper_input = si
        self._scraper = sc
        return sc

    def needed(self, r: dict) -> bool:
        d = r.get("description")
        if isinstance(d, str) and len(d) > 200:
            return False
        with self.lock:
            return _hash(r) not in self.known

    def _fetch(self, r: dict) -> dict:
        sc, url = self._get(), str(r.get("job_url") or "")
        if self.site == "linkedin":
            m = re.search(r"(\d{6,})", str(r.get("id") or "")) or re.search(r"/jobs/view/(?:[^/?]*-)?(\d+)", url)
            d = sc._get_job_details(m.group(1)) if m else {}  # noqa: SLF001
            out = {k: d.get(k) for k in ("description", "company_industry", "job_level", "job_function", "company_logo")}
            comp = d.get("compensation")
            if comp is not None and not _float(r.get("min_amount")):
                out.update(interval=comp.interval.value if comp.interval else None, min_amount=comp.min_amount,
                           max_amount=comp.max_amount, currency=comp.currency, salary_source="direct_data")
            if d.get("job_type") and not _str(r.get("job_type")):
                out["job_type"] = ", ".join(t.value[0] for t in d["job_type"])
            return out
        if self.site == "zip_recruiter":
            return sc._fetch_details(url)  # noqa: SLF001 - description, website, HQ, size, industry
        if self.site == "glassdoor":
            m = re.search(r"jl=(\d+)", url)
            return {"description": sc._fetch_job_description(m.group(1)) if m else None}  # noqa: SLF001
        return {}

    def fill(self, rows: list[dict]) -> None:
        from jobspy.util import extract_emails_from_text

        for r in rows:
            if self.stopped or not self.needed(r):
                continue
            if self.n + self.n_prev >= self.max_n:
                self.stopped = f"cap {self.max_n} postings/run reached"
                break
            if self.t + self.t_prev >= self.max_s:
                self.stopped = f"time cap {self.max_s:.0f}s reached"
                break
            t0 = time.time()
            try:
                d = self._fetch(r)
            except Exception as e:  # noqa: BLE001
                log.debug("%s detail failed: %s", self.site, e)
                d = {}
            self.n += 1
            desc = d.get("description")
            if isinstance(desc, str) and desc.strip():
                self.ok += 1
                self.empty_streak = 0
                for k, v in d.items():
                    if v not in (None, "") and (k == "description" or not _str(r.get(k))):
                        r[k] = v
                em = extract_emails_from_text(desc)
                if em:
                    have = (_str(r.get("emails")) or "").split(", ")
                    r["emails"] = ", ".join(x for x in dict.fromkeys([*have, *em]) if x)
                with self.lock:
                    self.known.add(_hash(r))
            else:
                self.empty_streak += 1
                if self.empty_streak >= 5:
                    self.stopped = "5 empty answers in a row (rate-limited?)"
            time.sleep(self.delay)
            self.t += time.time() - t0
        self.spent[self.site] = {"n": self.n + self.n_prev, "t": self.t + self.t_prev}

    def summary(self) -> str:
        return f"details {self.ok}/{self.n} in {self.t:.0f}s" + (f" ({self.stopped})" if self.stopped else "")


def _hash(r: dict) -> str:
    from ..normalize import dedupe_hash

    loc = r.get("location")
    return dedupe_hash(r.get("company") if isinstance(r.get("company"), str) else "", str(r.get("title") or ""),
                       loc if isinstance(loc, str) else "")


def extras(r: dict) -> dict:
    """Employer/posting fields JobSpy returns besides the core ones (stored in jobs.* columns;
    used by aggregator/enrich.py as the first, free source of company info)."""
    emails = r.get("emails")
    if isinstance(emails, str):
        emails = [e for e in re.split(r"[,;\s]+", emails) if e]
    emails = [e.strip().lower().rstrip(".") for e in emails] if isinstance(emails, (list, tuple)) else []
    return {
        "job_url_direct": _str(r.get("job_url_direct")),
        "emails": ",".join(dict.fromkeys(e for e in emails if "@" in e)) or None,
        "company_url": _str(r.get("company_url")),
        "company_url_direct": _str(r.get("company_url_direct")),
        "company_addresses": _str(r.get("company_addresses")),
        "company_industry": _str(r.get("company_industry")),
        "company_num_employees": _str(r.get("company_num_employees")),
        "company_revenue": _str(r.get("company_revenue")),
        "company_description": _str(r.get("company_description")),
        "company_logo": _str(r.get("company_logo")),
        "company_rating": _float(r.get("company_rating")),
        "company_reviews_count": _int(r.get("company_reviews_count")),
        "job_level": _str(r.get("job_level")),
        "job_function": _str(r.get("job_function")),
        "listing_type": _str(r.get("listing_type")),
        "salary_source": _str(r.get("salary_source")),
        "skills": _str(r.get("skills")),
        "experience_range": _str(r.get("experience_range")),
        "vacancy_count": _int(r.get("vacancy_count")),
        "work_from_home_type": _str(r.get("work_from_home_type")),
    }


def _row_to_job(site: str, r: dict) -> dict:
    loc = r.get("location") or ""
    if isinstance(loc, float):
        loc = ""
    remote = r.get("is_remote")
    remote = bool(remote) if remote is not None and remote == remote else is_remote_text(loc, r.get("title"))
    desc = r.get("description")
    desc = desc if isinstance(desc, str) else ""
    url = r.get("job_url_direct") if isinstance(r.get("job_url_direct"), str) and site == "google" else r.get("job_url")
    cur = r.get("currency")
    interval = r.get("interval")
    jt = r.get("job_type")
    return {
        "source_job_id": str(r.get("id") or ""),
        "company": r.get("company") if isinstance(r.get("company"), str) else "",
        "title": str(r.get("title") or "").strip(),
        "location": loc,
        "remote": remote,
        "salary_min": num(r.get("min_amount")),
        "salary_max": num(r.get("max_amount")),
        "salary_currency": cur if isinstance(cur, str) else None,
        "salary_interval": interval if isinstance(interval, str) else None,
        "job_type": jt if isinstance(jt, str) else None,
        "url": url if isinstance(url, str) else None,
        "description": desc,
        "posted_at": to_iso(r.get("date_posted")),
        **extras(r),
    }


def default_plan(cfg: dict) -> dict:
    """The Atlanta (main) track: every enabled site, search.location +distance."""
    s, j = cfg["search"], cfg["jobspy"]
    roles = [r for r in s.get("roles") or [] if r]
    return {
        "sites": [x for x in SITES if (j.get("sites") or {}).get(x)],
        "terms": roles or [t for t in j.get("search_terms") or [] if t] or [""],
        "location": s["location"],
        "distance": s["distance_miles"],
        "is_remote": bool(s.get("remote_only")),
        "hours_old": s.get("hours_old"),
        "results_wanted": j.get("results_wanted_by_site") or j["results_wanted"],
        "results_wanted_default": j["results_wanted"],
        "fetch_description_sites": j.get("fetch_description_sites"),
        "details": j.get("details"),
    }


def google_query(term: str, plan: dict, loc: str | None, j: dict) -> str:
    """Google Jobs ignores every structured parameter: JobSpy only uses google_search_term, which
    must read like what you'd type into Google ('nurse jobs near Atlanta, GA since yesterday')."""
    h = plan.get("hours_old") or 0
    when = ("since yesterday" if h <= 24 else "in the last 3 days" if h <= 72
            else "in the last week" if h <= 168 else "in the last month") if h else ""
    where = "remote" if plan.get("is_remote") else (f"near {loc}" if loc else "")
    tpl = j.get("google_search_template") or "{term} jobs {where} {when}"
    return re.sub(r"\s+", " ", tpl.format(term=term or "", where=where, when=when)).strip()


def scrape_kwargs(site: str, term: str, plan: dict, j: dict) -> dict:
    """Every scrape_jobs() option, resolved for one site + term (plan = track overrides)."""
    loc = plan["location"].get(site) if isinstance(plan["location"], dict) else plan["location"]
    rw = per_site(plan.get("results_wanted"), site, plan.get("results_wanted_default") or j.get("results_wanted") or 40)
    kw = dict(
        site_name=[site],
        search_term=term,
        location=loc or None,
        distance=plan.get("distance") or 50,
        is_remote=bool(plan.get("is_remote")),
        job_type=per_site(plan.get("job_type", j.get("job_type")), site),        # fulltime|parttime|internship|contract|None
        easy_apply=per_site(plan.get("easy_apply", j.get("easy_apply")), site),  # True hides employer-hosted (external) apply links
        results_wanted=int(rw),
        hours_old=plan.get("hours_old"),
        country_indeed=j.get("country_indeed") or "usa",
        offset=int(per_site(j.get("offset"), site, 0) or 0),
        fetch_description=detail_mode(site, plan, j) == "search",
        enforce_annual_salary=bool(j.get("enforce_annual_salary", True)),
        description_format=j.get("description_format") or "markdown",
        verbose=int(j.get("verbose", 0) or 0),
    )
    if site == "linkedin":
        ids = plan.get("linkedin_company_ids", j.get("linkedin_company_ids"))
        if ids:
            kw["linkedin_company_ids"] = [int(x) for x in ids]
    if site == "google":
        kw["google_search_term"] = google_query(term, plan, loc, j)
    if j.get("proxies"):
        kw["proxies"] = j["proxies"]
    if j.get("ca_cert"):
        kw["ca_cert"] = j["ca_cert"]
    if j.get("user_agent"):
        kw["user_agent"] = j["user_agent"]  # JobSpy 1.2 only applies it to Glassdoor
    return kw


def _run_site(site: str, plan: dict, cfg: dict, on_result) -> None:
    from jobspy import scrape_jobs  # imported lazily: heavy import

    j = cfg["jobspy"]
    det = _Details(site, j, plan.get("known_described"), plan.setdefault("_lock", threading.Lock()),
                   plan.get("detail_spent")) if detail_mode(site, plan, j) == "new" else None
    consecutive_fail = 0
    for term in plan["terms"]:
        if consecutive_fail >= j["max_consecutive_failures"]:
            on_result(site, term, "skipped", [], f"skipped after {consecutive_fail} consecutive failures")
            continue
        kwargs = scrape_kwargs(site, term, plan, j)
        t0 = time.time()
        err = None
        rows: list[dict] = []
        try:
            df = scrape_jobs(**kwargs)
            rows = df.to_dict("records") if df is not None and len(df) else []
        except Exception as e:  # noqa: BLE001 - any scraper error is non-fatal
            err = f"{type(e).__name__}: {str(e)[:300]}"
        internal = _capture.take(_LOGGER_NAMES[site])
        if not err and internal and not rows:
            err = "; ".join(dict.fromkeys(internal))[:400]
        t_search = time.time() - t0
        if det and rows:
            n0, ok0 = det.n, det.ok
            keep = plan.get("keep")  # only postings the track will keep are worth a detail request
            det.fill([r for r in rows if keep is None or keep(site, _row_to_job(site, r))])
            extra = f"+{det.ok - ok0}/{det.n - n0} details"
        else:
            extra = ""
        jobs = [_row_to_job(site, r) for r in rows]
        status = "error" if err and not jobs else ("ok" if jobs else "empty")
        if status == "error":
            consecutive_fail += 1
        else:
            consecutive_fail = 0
        log.info("%-13s %-26r %-6s %3d rows in %.1fs %s %s", site, term, status, len(jobs), t_search, extra, err or "")
        on_result(site, term, status, jobs, err)
        time.sleep(1.0)  # be polite between queries
    if det:
        log.info("%-13s %s", site, det.summary())
        plan.setdefault("detail_stats", {})[site] = {"fetched": det.n, "described": det.ok, "seconds": round(det.t, 1),
                                                     "stopped": det.stopped or None}


def fetch(cfg: dict, on_result, plan: dict | None = None) -> None:
    """Run every site in the plan; on_result(site, term, status, jobs, error) is called per query."""
    plan = plan or default_plan(cfg)
    sites = [s for s in SITES if s in plan["sites"]]
    if not sites:
        return
    lock = threading.Lock()

    def safe_cb(*a):
        with lock:
            on_result(*a)

    with ThreadPoolExecutor(max_workers=len(sites)) as ex:
        futs = [ex.submit(_run_site, site, plan, cfg, safe_cb) for site in sites]
        for f in futs:
            try:
                f.result()
            except Exception as e:  # noqa: BLE001
                log.exception("site worker crashed: %s", e)
