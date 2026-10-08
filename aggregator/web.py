"""Local web UI: FastAPI + Jinja2 + htmx (vendored, no CDN needed)."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, enrich, followups as fu
from .config import load_config
from .matching import tfidf_scores

HERE = Path(__file__).parent
cfg = load_config()
app = FastAPI(title="Job Aggregator")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
PAGE_SIZE = 50
SOURCES = ["greenhouse", "lever", "ashby", "indeed", "linkedin", "google", "zip_recruiter", "glassdoor"]


def _conn():
    return db.connect(cfg)


def _fmt_salary(r) -> str:
    lo, hi, cur = r["salary_min"], r["salary_max"], r["salary_currency"] or "USD"
    if lo is None and hi is None:
        return ""
    sym = "$" if cur == "USD" else f"{cur} "
    f = lambda v: f"{sym}{v/1000:.0f}k" if v and v >= 1000 else (f"{sym}{v:.2f}" if v else "?")
    s = f(lo) if lo == hi or hi is None else f"{f(lo)}–{f(hi)}"
    iv = (r["salary_interval"] or "").lower()
    return s + ("/hr" if iv.startswith("hour") else "")


REGIONS = ["US", "Worldwide", "North America", "Canada", "LATAM", "UK", "Ireland", "Europe", "EMEA",
           "India", "APAC", "Australia/NZ", "Middle East/Africa", "Unspecified"]


def search_jobs(q="", source="", remote="", company="", location="", days=0, salary_only=False,
                sort="date", page=1, track="", region="") -> dict:
    where, params = [], []
    tokens = [t for t in re.findall(r"[\w+#.-]+", q.lower()) if len(t) > 1]
    for t in tokens:
        where.append("(lower(title) LIKE ? OR lower(company) LIKE ? OR lower(description) LIKE ?)")
        params += [f"%{t}%"] * 3
    if source:
        where.append("(source = ? OR (',' || seen_on || ',') LIKE ?)")
        params += [source, f"%,{source},%"]
    if remote in ("1", "0"):
        where.append("remote = ?")
        params.append(int(remote))
    if track:
        where.append("(',' || track || ',') LIKE ?")
        params.append(f"%,{track},%")
    if region:
        # remote_region is a ", "-joined list (e.g. "US, Canada"); match whole entries only
        where.append("remote = 1 AND (', ' || remote_region || ',') LIKE ?")
        params.append(f"%, {region},%")
    if company:
        where.append("lower(company) LIKE ?")
        params.append(f"%{company.lower()}%")
    if location:
        where.append("lower(location) LIKE ?")
        params.append(f"%{location.lower()}%")
    if days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
        where.append("COALESCE(posted_at, first_seen_at) >= ?")
        params.append(cutoff)
    if salary_only:
        where.append("(salary_min IS NOT NULL OR salary_max IS NOT NULL)")
    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    conn = _conn()
    total = conn.execute(f"SELECT COUNT(*) FROM jobs {wsql}", params).fetchone()[0]
    cols = ("id, source, seen_on, company, title, location, remote, salary_min, salary_max, salary_currency, "
            "salary_interval, job_type, url, posted_at, first_seen_at, score, track, remote_region")
    offset = (max(page, 1) - 1) * PAGE_SIZE
    if sort == "score" and q.strip():
        # relevance to the search box: TF-IDF over the (bounded) matching set
        rows = conn.execute(f"SELECT {cols}, description FROM jobs {wsql} ORDER BY posted_at DESC LIMIT 3000", params).fetchall()
        rows = [dict(r) for r in rows]
        docs = [f"{r['title']} " * 3 + f"{r['company']} {(r['description'] or '')[:3000]}" for r in rows]
        for r, s in zip(rows, tfidf_scores(q, docs)):
            r["rel"] = s
        rows.sort(key=lambda r: r["rel"], reverse=True)
        rows = rows[offset: offset + PAGE_SIZE]
    else:
        order = {
            "date": "COALESCE(posted_at, first_seen_at) DESC",
            "score": "score DESC, COALESCE(posted_at, first_seen_at) DESC",
            "salary": "COALESCE(salary_max, salary_min, -1) DESC",
            "company": "lower(company) ASC, posted_at DESC",
        }.get(sort, "COALESCE(posted_at, first_seen_at) DESC")
        rows = [dict(r) for r in conn.execute(
            f"SELECT {cols} FROM jobs {wsql} ORDER BY {order} LIMIT ? OFFSET ?", params + [PAGE_SIZE, offset])]
        for r in rows:
            r["rel"] = r["score"]
    qualified = fu.qualified_ids(conn)
    conn.close()
    for r in rows:
        r["qualified"] = r["id"] in qualified
        r["salary"] = _fmt_salary(r)
        r["posted"] = (r["posted_at"] or "")[:10]
        r.pop("description", None)
    return {"total": total, "page": page, "pages": max(1, -(-total // PAGE_SIZE)), "rows": rows}


def _facets():
    conn = _conn()
    sources = conn.execute("SELECT source, COUNT(*) n FROM jobs GROUP BY source ORDER BY n DESC").fetchall()
    companies = [r[0] for r in conn.execute(
        "SELECT company FROM jobs WHERE company <> '' GROUP BY company ORDER BY COUNT(*) DESC LIMIT 300")]
    total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    tracks = [(t, conn.execute("SELECT COUNT(*) FROM jobs WHERE (',' || track || ',') LIKE ?", (f"%,{t},%",)).fetchone()[0])
              for t in ("atlanta", "remote_ai")]
    regions = [(r, conn.execute("SELECT COUNT(*) FROM jobs WHERE remote = 1 AND (', ' || remote_region || ',') LIKE ?",
                                (f"%, {r},%",)).fetchone()[0]) for r in REGIONS]
    last = conn.execute("SELECT summary_json FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
    conn.close()
    return {"sources": sources, "companies": companies, "total": total, "tracks": tracks,
            "regions": [x for x in regions if x[1]],
            "last_run": json.loads(last[0]) if last else None}


def _params(q, source, remote, company, location, days, salary_only, sort, page, track="", region=""):
    return dict(q=q, source=source, remote=remote, company=company, location=location,
                days=days, salary_only=salary_only, sort=sort, page=page, track=track, region=region)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, q: str = "", source: str = "", remote: str = "", company: str = "",
          location: str = "", days: int = 0, salary_only: bool = False, sort: str = "date", page: int = 1,
          track: str = "", region: str = ""):
    p = _params(q, source, remote, company, location, days, salary_only, sort, page, track, region)
    return templates.TemplateResponse(request, "index.html", {
        "p": p, "res": search_jobs(**p), "f": _facets(), "cfg": cfg})


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = "", source: str = "", remote: str = "", company: str = "",
           location: str = "", days: int = 0, salary_only: bool = False, sort: str = "date", page: int = 1,
          track: str = "", region: str = ""):
    p = _params(q, source, remote, company, location, days, salary_only, sort, page, track, region)
    if "hx-request" not in request.headers:  # direct visit / reload of a pushed URL -> full page
        return index(request, **p)
    return templates.TemplateResponse(request, "_results.html", {"p": p, "res": search_jobs(**p)})


@app.get("/api/jobs")
def api_jobs(q: str = "", source: str = "", remote: str = "", company: str = "", location: str = "",
             days: int = 0, salary_only: bool = False, sort: str = "date", page: int = Query(1, ge=1),
             track: str = "", region: str = ""):
    return JSONResponse(search_jobs(**_params(q, source, remote, company, location, days, salary_only, sort, page, track, region)))


@app.get("/api/stats")
def api_stats():
    f = _facets()
    return {"total": f["total"], "by_source": {r[0]: r[1] for r in f["sources"]}, "last_run": f["last_run"]}


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------------------------------------------------------------- follow-ups
# These endpoints only create/edit/approve DRAFTS. Nothing here sends email;
# sending is CLI-only: `python -m aggregator send-approved`.

QUALIFY_BTN = ('<button class="qbtn {cls}" hx-post="/jobs/{id}/{action}" hx-swap="outerHTML" '
               'title="{tip}">{label}</button>')


def _qbtn(job_id, qualified):
    if qualified:
        return QUALIFY_BTN.format(cls="on", id=job_id, action="unqualify", label="✓ Qualified",
                                  tip="Qualified: 3 follow-up drafts queued. Click to un-qualify (skips unsent drafts).")
    return QUALIFY_BTN.format(cls="", id=job_id, action="qualify", label="Qualify",
                              tip="Mark qualified and generate 3 follow-up drafts (nothing is sent)")


@app.post("/jobs/{job_id}/qualify", response_class=HTMLResponse)
def qualify_job(job_id: str):
    conn = _conn()
    try:
        fu.qualify(conn, cfg, job_id, by="manual")
    except KeyError:
        raise HTTPException(404, "job not found")
    finally:
        conn.close()
    return _qbtn(job_id, True)


@app.post("/jobs/{job_id}/unqualify", response_class=HTMLResponse)
def unqualify_job(job_id: str):
    conn = _conn()
    fu.unqualify(conn, job_id)
    conn.close()
    return _qbtn(job_id, False)


def _queue_data(conn) -> dict:
    data = fu.queue(conn)
    data["ci"] = {}
    for f in data["due"] + data["upcoming"]:
        if f["job_id"] not in data["ci"]:
            try:
                data["ci"][f["job_id"]] = enrich.brief(conn, f["job_id"])
            except Exception:  # noqa: BLE001 - company info is optional
                pass
    return data


def _queue_html(request, msg=""):
    conn = _conn()
    data = _queue_data(conn)
    conn.close()
    return templates.TemplateResponse(request, "_queue.html", {"qd": data, "msg": msg, "cfg": cfg})


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_page(request: Request, job_id: str):
    """One job + its company info (website, careers/apply link, published hiring email + source)."""
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, "job not found")
        job = dict(row)
        job["salary"] = _fmt_salary(row)
        lead = conn.execute("SELECT * FROM leads WHERE job_id=?", (job_id,)).fetchone()
        ci = enrich.brief(conn, job_id)
        qualified = job_id in fu.qualified_ids(conn)
    finally:
        conn.close()
    return templates.TemplateResponse(request, "job.html", {"job": job, "lead": dict(lead) if lead else None, "ci": ci,
                                                           "qbtn": _qbtn(job_id, qualified), "cfg": cfg})


@app.post("/jobs/{job_id}/enrich", response_class=HTMLResponse)
def job_enrich(request: Request, job_id: str):
    """Look up company info for one job now (free public sources; never sends anything)."""
    conn = _conn()
    try:
        if not conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone():
            raise HTTPException(404, "job not found")
        done = bool(conn.execute("SELECT enriched_at FROM jobs WHERE id=?", (job_id,)).fetchone()[0])
        st = enrich.enrich_jobs(conn, cfg, [job_id], budget_s=120, max_companies=1, force=done)
        ci = enrich.brief(conn, job_id)
    finally:
        conn.close()
    msg = (f"Looked up in {st.get('seconds', 0):.0f} s"
           + (f"; lead contact filled with {st['contacts_set'][0]['email']}" if st.get("contacts_set") else "")
           + (f"; problem: {st['errors'][0]}" if st.get("errors") else ""))
    return templates.TemplateResponse(request, "_company.html", {"ci": ci, "msg": msg})


@app.get("/api/jobs/{job_id}/company")
def api_job_company(job_id: str):
    conn = _conn()
    try:
        return JSONResponse(enrich.brief(conn, job_id))
    finally:
        conn.close()


@app.get("/followups", response_class=HTMLResponse)
def followups_page(request: Request):
    conn = _conn()
    data = _queue_data(conn)
    conn.close()
    return templates.TemplateResponse(request, "followups.html", {"qd": data, "msg": "", "cfg": cfg})


@app.post("/followups/{fid}/{action}", response_class=HTMLResponse)
def followup_action(request: Request, fid: str, action: str, subject: str = Form(""), body: str = Form("")):
    conn = _conn()
    msg = ""
    try:
        if action == "save":
            fu.edit(conn, fid, subject, body)
            msg = "Draft saved."
        elif action == "approve":
            if subject or body:
                fu.edit(conn, fid, subject, body)
            fu.approve(conn, fid)
            msg = "Approved. It will go out only when you run: python -m aggregator send-approved"
        elif action == "unapprove":
            fu.unapprove(conn, fid)
            msg = "Moved back to draft."
        elif action == "skip":
            fu.skip(conn, fid)
            msg = "Skipped."
        else:
            raise HTTPException(400, "unknown action")
    except ValueError as e:
        msg = f"⚠ {e}"
    finally:
        conn.close()
    return _queue_html(request, msg)


@app.post("/leads/{job_id}/contact", response_class=HTMLResponse)
def lead_contact(request: Request, job_id: str, contact_name: str = Form(""), contact_email: str = Form("")):
    conn = _conn()
    try:
        fu.set_contact(conn, cfg, job_id, contact_name, contact_email)
        msg = "Contact saved; unedited drafts refreshed."
    except ValueError as e:
        msg = f"⚠ {e}"
    finally:
        conn.close()
    return _queue_html(request, msg)


@app.post("/leads/{job_id}/replied", response_class=HTMLResponse)
def lead_replied(request: Request, job_id: str):
    conn = _conn()
    fu.mark_replied(conn, job_id)
    conn.close()
    return _queue_html(request, "Marked replied; remaining touches stopped.")


@app.get("/api/followups")
def api_followups():
    conn = _conn()
    data = fu.queue(conn)
    conn.close()
    return data
