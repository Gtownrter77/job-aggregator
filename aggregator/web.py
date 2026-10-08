"""Local web UI: FastAPI + Jinja2 + htmx (vendored, no CDN needed)."""
from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import access, db, enrich, followups as fu
from .config import load_config
from .matching import tfidf_scores

HERE = Path(__file__).parent
cfg = load_config()
app = FastAPI(title="Job Aggregator")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals["is_local"] = lambda request: access.is_loopback(request.client.host if request.client else None)
PAGE_SIZE = 50
SOURCES = ["greenhouse", "lever", "ashby", "indeed", "linkedin", "google", "zip_recruiter", "glassdoor"]


# ---------------------------------------------------------------- LAN access token
# Requests from this computer (127.0.0.1 / ::1) are always allowed. Anything else
# (the phone app, another device on your Wi-Fi) must present the access token when
# one is configured (see aggregator/access.py). /healthz stays open so the phone
# app's "Test connection" can tell "server unreachable" apart from "wrong token".
ACCESS_TOKEN = access.resolve_token(cfg)
OPEN_PATHS = {"/healthz"}

TOKEN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Access token · Job Aggregator</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;margin:0;padding:24px;background:#f7f7f8;color:#1b1b1f}}
form{{max-width:420px;margin:40px auto;background:#fff;border:1px solid #e4e4e9;border-radius:10px;padding:18px}}
input,button{{font:inherit;padding:10px;width:100%;margin-top:8px;border:1px solid #e4e4e9;border-radius:7px}}
button{{background:#2952cc;color:#fff;border-color:#2952cc}}.err{{color:#9b2a17}}</style></head><body>
<form method="get" action="/"><b>Job Aggregator: access token required</b>
<p>{msg}</p><p style="font-size:14px;color:#6b6b76">On the computer running the aggregator, run
<code>python -m aggregator token</code> or open <code>http://localhost:{port}/phone</code> to see it.</p>
<input name="token" placeholder="xxxx-xxxx-xxxx-xxxx" autocomplete="off" autocapitalize="none" autofocus>
<button>Continue</button></form></body></html>"""


@app.middleware("http")
async def access_token_gate(request: Request, call_next):
    token = ACCESS_TOKEN
    client = request.client.host if request.client else None
    if not token or request.url.path in OPEN_PATHS or access.is_loopback(client):
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    from_query = request.query_params.get("token")
    supplied_fresh = [request.headers.get("x-access-token"),
                      auth[7:] if auth.lower().startswith("bearer ") else None, from_query]
    if access.check(token, [request.cookies.get(access.COOKIE)]):
        return await call_next(request)
    if access.check(token, supplied_fresh):
        if from_query and request.method == "GET":  # drop the token from the URL bar / history
            q = urlencode([(k, v) for k, v in request.query_params.multi_items() if k != "token"])
            resp = RedirectResponse(request.url.path + (f"?{q}" if q else ""), status_code=303)
        else:
            resp = await call_next(request)
        resp.set_cookie(access.COOKIE, token, max_age=365 * 86400, httponly=True, samesite="lax")
        return resp
    msg = "That token is not right." if any(supplied_fresh) or request.cookies.get(access.COOKIE) else \
        "This server only accepts devices that know its access token."
    if request.url.path.startswith("/api/") or "hx-request" in request.headers:
        return JSONResponse({"error": "access token required"}, status_code=401)
    return HTMLResponse(TOKEN_PAGE.format(msg=html.escape(msg), port=request.url.port or cfg["server"]["port"]),
                        status_code=401)


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
    return {"ok": True, "app": "job-aggregator", "token_required": bool(ACCESS_TOKEN)}


@app.get("/phone", response_class=HTMLResponse)
def phone_page(request: Request):
    """How to connect the Android app. The token is only shown to this computer."""
    local = access.is_loopback(request.client.host if request.client else None)
    port = request.url.port or cfg["server"]["port"]
    return templates.TemplateResponse(request, "phone.html", {
        "local": local, "port": port, "ips": access.lan_ips() if local else [],
        "token": ACCESS_TOKEN if local else None, "token_source": access.token_source(cfg) if local else "",
        "lan_bound": os.environ.get("AGGREGATOR_BIND_HOST", ""), "cfg": cfg})


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
