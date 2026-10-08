"""JobSpy (github.com/speedyapply/JobSpy) wrapper: Indeed, LinkedIn, Google, ZipRecruiter, Glassdoor.

Each site runs in its own thread, one search term at a time, fully isolated:
a failing site is logged (with JobSpy's own error message) and skipped,
never aborting the rest of the fetch.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from ..normalize import is_remote_text, num, to_iso

log = logging.getLogger("aggregator.jobspy")

SITES = ["indeed", "linkedin", "google", "zip_recruiter", "glassdoor"]
_LOGGER_NAMES = {
    "indeed": "JobSpy:Indeed", "linkedin": "JobSpy:LinkedIn", "google": "JobSpy:Google",
    "zip_recruiter": "JobSpy:ZipRecruiter", "glassdoor": "JobSpy:Glassdoor",
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


def detail_sites(site: str, plan: dict, j: dict) -> bool:
    """Fetch the full posting for this site at search time? (jobspy fetch_description)"""
    if j.get("fetch_description") or (site == "linkedin" and j.get("linkedin_fetch_description")):
        return True
    sites = plan.get("fetch_description_sites")
    if sites is None:
        sites = j.get("fetch_description_sites") or []
    return site in sites


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
        "results_wanted": j["results_wanted"],
        "fetch_description_sites": j.get("fetch_description_sites"),
    }


def _run_site(site: str, plan: dict, cfg: dict, on_result) -> None:
    from jobspy import scrape_jobs  # imported lazily: heavy import

    j = cfg["jobspy"]
    loc = plan["location"].get(site) if isinstance(plan["location"], dict) else plan["location"]
    consecutive_fail = 0
    for term in plan["terms"]:
        if consecutive_fail >= j["max_consecutive_failures"]:
            on_result(site, term, "skipped", [], f"skipped after {consecutive_fail} consecutive failures")
            continue
        kwargs = dict(
            site_name=[site],
            search_term=term,
            location=loc or None,
            distance=plan.get("distance"),
            results_wanted=plan["results_wanted"],
            hours_old=plan.get("hours_old"),
            country_indeed=j["country_indeed"],
            is_remote=bool(plan.get("is_remote")),
            # per-site detail fetch (one extra request per posting): description, emails,
            # employer website/HQ. Sites not listed get details on demand in aggregator/enrich.py.
            fetch_description=detail_sites(site, plan, j),
            enforce_annual_salary=True,
            description_format="markdown",
            verbose=0,
        )
        if site == "google":
            # Google Jobs ignores structured params; it needs a natural-language query.
            when = "since yesterday" if (plan.get("hours_old") or 999) <= 24 else "in the last week"
            where = "remote" if plan.get("is_remote") else f"near {loc}"
            kwargs["google_search_term"] = f"{term} jobs {where} {when}"
        if j.get("proxies"):
            kwargs["proxies"] = j["proxies"]
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
        jobs = [_row_to_job(site, r) for r in rows]
        status = "error" if err and not jobs else ("ok" if jobs else "empty")
        if status == "error":
            consecutive_fail += 1
        else:
            consecutive_fail = 0
        log.info("%-13s %-26r %-6s %3d rows in %.1fs %s", site, term, status, len(jobs), time.time() - t0, err or "")
        on_result(site, term, status, jobs, err)
        time.sleep(1.0)  # be polite between queries


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
