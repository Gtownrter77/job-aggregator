"""Public, unauthenticated ATS job-board APIs: Greenhouse, Lever, Ashby."""
from __future__ import annotations

import asyncio
import logging
import re

import httpx

from ..normalize import html_to_text, is_remote_text, num, parse_salary_text, to_iso

log = logging.getLogger("aggregator.ats")

URLS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true",
}
VAGUE_LOC = re.compile(r"remote|united states|multiple|various|anywhere|north america|^\s*(us|usa|hybrid)\s*$", re.I)
HEADERS = {"User-Agent": "open-job-aggregator/0.1 (+https://github.com/speedyapply/JobSpy)"}


# --- per-ATS parsers: raw posting -> (job dict, [all location strings]) -------

def _greenhouse(company: str, j: dict) -> tuple[dict, list[str]]:
    loc = (j.get("location") or {}).get("name") or ""
    # Greenhouse "offices" is an internal mapping (e.g. a Toronto posting tied to the
    # Atlanta office), so only consult it when the posted location is vague.
    locs = [loc]
    remote = is_remote_text(loc, j.get("title"))
    if not loc or VAGUE_LOC.search(loc):
        locs += [o.get("location") or o.get("name") or "" for o in j.get("offices") or []]
    desc = html_to_text(j.get("content"))
    smin, smax, cur = parse_salary_text(desc)
    return {
        "source_job_id": str(j.get("id")),
        "company": j.get("company_name") or company,
        "title": (j.get("title") or "").strip(),
        "location": loc,
        "remote": remote,
        "salary_min": smin, "salary_max": smax, "salary_currency": cur,
        "salary_interval": "yearly" if smin else None,
        "job_type": None,
        "url": j.get("absolute_url"),
        "description": desc,
        "posted_at": to_iso(j.get("first_published") or j.get("updated_at")),
    }, locs


def _lever(company: str, j: dict) -> tuple[dict, list[str]]:
    cats = j.get("categories") or {}
    loc = cats.get("location") or ""
    locs = [loc] + list(cats.get("allLocations") or [])
    parts = [j.get("descriptionPlain") or ""]
    for lst in j.get("lists") or []:
        parts.append(f"{lst.get('text','')}\n{html_to_text(lst.get('content'))}")
    parts.append(j.get("additionalPlain") or "")
    desc = "\n\n".join(p for p in parts if p).strip()
    sr = j.get("salaryRange") or {}
    smin, smax, cur = num(sr.get("min")), num(sr.get("max")), sr.get("currency")
    if smin is None:
        smin, smax, cur = parse_salary_text(desc)
    return {
        "source_job_id": j.get("id"),
        "company": company,
        "title": (j.get("text") or "").strip(),
        "location": loc,
        "remote": (j.get("workplaceType") == "remote")
                   or (j.get("workplaceType") not in ("onsite", "on-site") and is_remote_text(*locs)),
        "salary_min": smin, "salary_max": smax, "salary_currency": cur,
        "salary_interval": (sr.get("interval") or ("yearly" if smin else None)),
        "job_type": cats.get("commitment"),
        "url": j.get("hostedUrl"),
        "description": desc,
        "posted_at": to_iso(j.get("createdAt")),
    }, locs


def _ashby(company: str, j: dict) -> tuple[dict, list[str]]:
    loc = j.get("location") or ""
    addr = ((j.get("address") or {}).get("postalAddress") or {})
    addr_s = ", ".join(x for x in [addr.get("addressLocality"), addr.get("addressRegion")] if x)
    locs = [loc, addr_s] + [s.get("location") or "" for s in j.get("secondaryLocations") or []]
    for s in j.get("secondaryLocations") or []:
        pa = ((s.get("address") or {}).get("postalAddress") or {})
        locs.append(", ".join(x for x in [pa.get("addressLocality"), pa.get("addressRegion")] if x))
    smin = smax = cur = interval = None
    comp = j.get("compensation") or {}
    for c in comp.get("summaryComponents") or []:
        if c.get("compensationType") == "Salary":
            smin, smax, cur = num(c.get("minValue")), num(c.get("maxValue")), c.get("currencyCode")
            interval = (c.get("interval") or "").replace("1 ", "").lower() or None
            break
    desc = j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml"))
    # Ashby "location" is often just "Atlanta"; add state for clearer display/dedupe
    display_loc = loc
    if addr_s and addr.get("addressRegion") and addr.get("addressRegion") not in loc:
        display_loc = f"{loc}, {addr.get('addressRegion')}" if loc and addr.get("addressLocality") == loc else (loc or addr_s)
    return {
        "source_job_id": j.get("id"),
        "company": company,
        "title": (j.get("title") or "").strip(),
        "location": display_loc,
        # Ashby isRemote is also true for Hybrid roles, so trust workplaceType first.
        "remote": (j.get("workplaceType") == "Remote")
                   or (j.get("workplaceType") is None and bool(j.get("isRemote")))
                   or (j.get("workplaceType") != "OnSite" and is_remote_text(*locs)),
        "salary_min": smin, "salary_max": smax, "salary_currency": cur,
        "salary_interval": interval,
        "job_type": j.get("employmentType"),
        "url": j.get("jobUrl"),
        "description": desc,
        "posted_at": to_iso(j.get("publishedAt")),
    }, locs


PARSERS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby}


def _extract(ats: str, payload) -> list[dict]:
    if ats == "lever":
        return payload if isinstance(payload, list) else []
    jobs = payload.get("jobs") or []
    if ats == "ashby":
        jobs = [j for j in jobs if j.get("isListed", True)]
    return jobs


async def fetch_company(client: httpx.AsyncClient, ats: str, company: dict) -> dict:
    """Returns {"ats","slug","name","status","jobs":[(job, locs)],"error"}."""
    slug = company["slug"]
    name = company.get("name") or slug
    url = URLS[ats].format(slug=slug)
    res = {"ats": ats, "slug": slug, "name": name, "status": "ok", "jobs": [], "error": None, "http": None}
    for attempt in range(4):
        try:
            r = await client.get(url)
            res["http"] = r.status_code
            if r.status_code == 429 or r.status_code >= 500:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            if r.status_code != 200:
                res.update(status="error", error=f"HTTP {r.status_code}")
                return res
            raw = _extract(ats, r.json())
            res["jobs"] = [PARSERS[ats](name, j) for j in raw]
            res["status"] = "ok" if raw else "empty"
            res["error"] = None  # clear any error from an earlier, retried attempt
            return res
        except Exception as e:  # network / JSON errors: retry then report
            res.update(status="error", error=f"{type(e).__name__}: {e}")
            await asyncio.sleep(1 + attempt)
    if res["status"] == "ok":
        res.update(status="error", error=f"HTTP {res['http']} after retries")
    return res


async def fetch_all(companies: dict[str, list[dict]], concurrency: int = 8, timeout: float = 45) -> list[dict]:
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(timeout=timeout, headers=HEADERS, follow_redirects=True) as client:
        async def run(ats, c):
            async with sem:
                r = await fetch_company(client, ats, c)
                log.info("%-10s %-22s %-6s %4d postings %s", ats, c["slug"], r["status"], len(r["jobs"]), r["error"] or "")
                return r
        tasks = [run(ats, c) for ats, lst in companies.items() for c in lst]
        return await asyncio.gather(*tasks)


async def verify_slugs(companies: dict[str, list[dict]], timeout: float = 30) -> list[tuple[str, str, int | None, int]]:
    """Return (ats, slug, http_status, n_jobs) for each configured slug."""
    out = []
    async with httpx.AsyncClient(timeout=timeout, headers=HEADERS, follow_redirects=True) as client:
        async def one(ats, c):
            try:
                r = await client.get(URLS[ats].format(slug=c["slug"]))
                n = len(_extract(ats, r.json())) if r.status_code == 200 else 0
                return ats, c["slug"], r.status_code, n
            except Exception:
                return ats, c["slug"], None, 0
        out = await asyncio.gather(*[one(a, c) for a, lst in companies.items() for c in lst])
    return list(out)
