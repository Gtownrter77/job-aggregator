"""Unattended run: fetch both tracks -> rescore -> auto-qualify a few strong NEW matches
(3 follow-up DRAFTS each, written by the local Ollama model, templates as fallback)
-> write a Markdown digest. Never sends, approves or schedules email.

Used by scripts/auto_run.sh (cron, 3x per weekday).
"""
from __future__ import annotations

import copy
import json
import os
import logging
import re
import shutil
import time
import traceback
from datetime import datetime, timedelta, timezone

from . import db, followups, llm
from .compose import _applicant, applicant_facts, display_company, facts_text, fit_level
from .config import resolve
from .enrich import md_lines

log = logging.getLogger("aggregator.auto")

_STOP = {"the", "and", "of", "a", "an", "for", "to", "in", "at", "with", "&", "-", "/", "|", "full", "time", "part",
         "remote", "hybrid", "onsite", "on-site", "ga", "atlanta", "sr", "sr.", "senior", "jr", "i", "ii", "iii"}


def _title_tokens(t: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (t or "").lower()) if w not in _STOP}


def _same_job(a: dict, b: dict) -> bool:
    """Same company and near-identical title (e.g. one req re-posted in several suburbs)."""
    ca = re.sub(r"[^a-z0-9]", "", display_company(a.get("company")).lower())
    cb = re.sub(r"[^a-z0-9]", "", display_company(b.get("company")).lower())
    if not ca or ca != cb:
        return False
    ta, tb = _title_tokens(a.get("title")), _title_tokens(b.get("title"))
    return bool(ta and tb) and len(ta & tb) / len(ta | tb) >= 0.5


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def pick_candidates(conn, cfg, since_utc: str) -> tuple[list[dict], list[dict]]:
    """(to_qualify, other_strong): strong, not-yet-qualified jobs first seen after since_utc."""
    a = cfg["auto"]
    app = _applicant(cfg)
    excl = re.compile(a["exclude_title_regex"], re.I) if a.get("exclude_title_regex") else None
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM jobs WHERE id NOT IN (SELECT job_id FROM leads) AND score >= ? AND first_seen_at >= ?"
        " ORDER BY score DESC LIMIT 300", (float(a["min_score"]), since_utc))]
    taken = [dict(r) for r in conn.execute("SELECT j.id, j.company, j.title FROM leads l JOIN jobs j ON j.id = l.job_id")]
    chosen, other, per_co = [], [], {}
    for j in rows:
        if excl and excl.search(j.get("title") or ""):
            continue
        j["fit"] = fit_level(app, j)
        if a.get("require_direct_fit", True) and j["fit"] != "direct":
            continue
        if any(_same_job(j, t) for t in taken + chosen):
            continue  # duplicate of something already qualified / picked
        co = display_company(j.get("company")).lower()
        if len(chosen) < int(a["max_qualify_per_run"]) and per_co.get(co, 0) < int(a["max_per_company_per_run"]):
            chosen.append(j)
            per_co[co] = per_co.get(co, 0) + 1
        elif not any(_same_job(j, o) for o in other):
            other.append(j)
    return chosen, other


def _fit_summary(conn, cfg, job: dict, facts: str) -> str | None:
    data = json.loads(job.get("llm_json") or "{}") if (job.get("llm_json") or "").startswith("{") else {}
    if data.get("fit_summary") and data.get("fit_model") == cfg["llm"]["model"] and data.get("fit_v") == 3:
        return data["fit_summary"]
    s = llm.fit_summary(cfg, job, facts)
    if s:
        data.update(fit_summary=s, fit_model=cfg["llm"]["model"], fit_v=3)
        with conn:
            conn.execute("UPDATE jobs SET llm_json=? WHERE id=?", (json.dumps(data), job["id"]))
    return s


def _md_job(j: dict, extra: str = "") -> str:
    where = j.get("location") or "?"
    if j.get("remote_region"):
        where += f" (remote: {j['remote_region']})"
    line = f"**{j.get('title')}** - {j.get('company')} - {where} - score {j.get('score', 0):.3f}"
    if j.get("fit"):
        line += f", fit: {j['fit']}"
    line += f" - [{(j.get('source') or 'link')}]({j.get('url') or ''})" if j.get("url") else " - (no link)"
    return line + extra


def _try_lock(fh) -> bool:
    """Non-blocking exclusive lock: fcntl on macOS/Linux, msvcrt on Windows."""
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:  # BlockingIOError / PermissionError
        return False


def _unlock(fh) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError:
        pass
    fh.close()


def run_auto(cfg: dict, fetch: bool = True, qualify: bool = True) -> dict:
    lock_path = resolve("logs/.auto_python.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = open(lock_path, "w")
    if not _try_lock(lock):
        log.warning("another auto run is in progress; exiting")
        return {"skipped": "locked"}

    t0 = time.time()
    started = datetime.now().astimezone()
    started_utc = _utc(started)
    a = cfg["auto"]
    res: dict = {"started": started.isoformat(timespec="seconds"), "fetch": {}, "errors": [], "source_errors": {}}

    # 1. fetch each track separately so one track's crash can't take down the other
    tracks = [t for t, tc in (cfg.get("tracks") or {}).items() if (tc or {}).get("enabled")]
    detail_spent: dict = {}  # JobSpy detail-fetch caps (jobspy.detail_max_*) are per run, shared by the tracks
    for t in (tracks if fetch else []):
        try:
            s = __import__("aggregator.fetcher", fromlist=["run_fetch"]).run_fetch(copy.deepcopy(cfg), only_tracks=[t],
                                                                                  detail_spent=detail_spent)
            res["fetch"][t] = {k: s.get(k) for k in ("seconds", "unique_this_run", "db_inserted_new", "db_updated_existing",
                                                    "db_pruned_stale_ats", "db_total", "jobspy_seconds_by_track", "jobspy_details")}
            for src, errs in (s.get("errors") or {}).items():
                if errs:
                    res["source_errors"].setdefault(src, []).extend(errs if isinstance(errs, list) else [str(errs)])
        except Exception as e:  # noqa: BLE001
            log.exception("fetch failed for track %s", t)
            res["errors"].append(f"fetch {t}: {type(e).__name__}: {e}")
            res["fetch"][t] = {"error": str(e)[:300]}

    conn = db.connect(cfg)
    # 2. rescore (fetch already does; harmless and needed with --no-fetch)
    try:
        from .matching import rescore_all
        rescore_all(cfg, conn)
    except Exception as e:  # noqa: BLE001
        log.exception("rescore failed")
        res["errors"].append(f"rescore: {e}")

    # 3. what's new
    new_rows = [dict(r) for r in conn.execute("SELECT * FROM jobs WHERE first_seen_at >= ? ORDER BY score DESC", (started_utc,))]
    res["new_total"] = len(new_rows)
    res["new_by_track"] = {t: sum(1 for j in new_rows if t in (j.get("track") or "").split(",")) for t in ("atlanta", "remote_ai")}

    ollama_up = llm.ollama_available(cfg, model=True) if cfg["llm"].get("enabled") else False
    res["ollama"] = f"{cfg['llm']['model']} up" if ollama_up else "DOWN/disabled (templates used)"

    # 4. auto-qualify a few strong new matches (drafts only)
    since = _utc(started - timedelta(hours=float(a["new_within_hours"])))
    chosen, other = pick_candidates(conn, cfg, since)
    res["qualified"], res["other_strong"] = [], other[:10]
    for j in (chosen if qualify else []):
        t1 = time.time()
        try:
            followups.qualify(conn, cfg, j["id"], by="auto")
            fu = [dict(r) for r in conn.execute("SELECT touch, scheduled_for, status, subject, body, generator FROM followups"
                                                " WHERE job_id=? ORDER BY touch", (j["id"],))]
            lead = dict(conn.execute("SELECT * FROM leads WHERE job_id=?", (j["id"],)).fetchone())
            res["qualified"].append({**j, "drafts": fu, "contact_email": lead.get("contact_email"),
                                     "draft_seconds": round(time.time() - t1, 1)})
        except Exception as e:  # noqa: BLE001
            log.exception("qualify failed for %s", j["id"])
            res["errors"].append(f"qualify {j['id']}: {e}")

    # 5. top matches (first seen in the window; include ones we just auto-qualified)
    qualified_now = {q["id"] for q in res["qualified"]}
    app = _applicant(cfg)
    if qualified_now:
        sql = ("SELECT * FROM jobs WHERE first_seen_at >= ? AND (id NOT IN (SELECT job_id FROM leads) OR id IN ("
               + ",".join("?" * len(qualified_now)) + ")) ORDER BY score DESC LIMIT ?")
        params = (since, *qualified_now, int(a["top_n"]) * 3)
    else:
        sql = "SELECT * FROM jobs WHERE first_seen_at >= ? AND id NOT IN (SELECT job_id FROM leads) ORDER BY score DESC LIMIT ?"
        params = (since, int(a["top_n"]) * 3)
    top = [dict(r) for r in conn.execute(sql, params)]
    excl = re.compile(a["exclude_title_regex"], re.I) if a.get("exclude_title_regex") else None
    dedup = []
    for j in top:
        if (excl and excl.search(j.get("title") or "")) or any(_same_job(j, d) for d in dedup):
            continue
        j["fit"] = fit_level(app, j)
        j["is_new"] = j["first_seen_at"] >= started_utc
        dedup.append(j)
    res["top"] = dedup[: int(a["top_n"])]

    # 5b. company info (website, careers/apply link, published hiring email + source) for the new
    # leads and the digest's matches; fills a lead's EMPTY contact only with a HIRING address
    # read verbatim on a cited page. Time-capped (enrich.max_seconds_per_run) and fail-soft.
    res["enrich"] = {}
    if (cfg.get("enrich") or {}).get("enabled", True):
        try:
            from . import enrich

            ids = [q["id"] for q in res["qualified"]] + [j["id"] for j in res["top"]] + [j["id"] for j in res["other_strong"][:5]]
            ids += [r[0] for r in conn.execute("SELECT job_id FROM leads WHERE status='active' AND COALESCE(contact_email,'')=''")]
            st = enrich.enrich_jobs(conn, cfg, ids)
            res["enrich"] = {k: st.get(k) for k in ("jobs", "companies_crawled", "companies_cached", "anonymous", "details_fetched",
                                                    "deferred", "contacts_set", "errors", "seconds", "http_requests", "searches",
                                                    "skipped")}
            for j in res["top"] + res["qualified"] + res["other_strong"]:
                j["company_info"] = enrich.brief(conn, j["id"])
            for q in res["qualified"]:
                lc = (q.get("company_info") or {}).get("lead_contact") or {}
                q["contact_email"], q["contact_source"] = lc.get("contact_email"), lc.get("contact_source")
        except Exception as e:  # noqa: BLE001
            log.exception("enrichment failed")
            res["errors"].append(f"enrich: {type(e).__name__}: {e}")

    if ollama_up and cfg["llm"].get("fit_summary"):
        af = applicant_facts(cfg)
        for j in res["top"] + res["qualified"]:
            try:
                j["fit_summary"] = _fit_summary(conn, cfg, j, facts_text(af, j, app))
            except Exception as e:  # noqa: BLE001
                log.warning("fit summary: %s", e)

    # 6. follow-up queue state
    q = followups.queue(conn)
    res["due"] = q["due"]
    res["blocked"] = [dict(r) for r in conn.execute(
        "SELECT j.id, j.title, j.company, j.url, l.qualified_by,"
        " (SELECT MIN(scheduled_for) FROM followups f WHERE f.job_id=l.job_id AND f.status IN ('draft','approved')) AS next_touch"
        " FROM leads l JOIN jobs j ON j.id=l.job_id WHERE l.status='active' AND (l.contact_email IS NULL OR l.contact_email='')"
        " ORDER BY next_touch")]
    try:
        from .enrich import brief

        for b in res["blocked"]:
            b["company_info"] = brief(conn, b["id"])
    except Exception as e:  # noqa: BLE001
        log.warning("blocked-lead company info: %s", e)
    res["upcoming_count"] = len(q["upcoming"])
    res["seconds"] = round(time.time() - t0, 1)
    conn.close()

    res["digest"] = write_digest(cfg, res, started)
    _unlock(lock)
    return res


def write_digest(cfg: dict, r: dict, started: datetime) -> str:
    out_dir = resolve(cfg["auto"].get("digest_dir") or "logs")
    out_dir.mkdir(parents=True, exist_ok=True)
    L = [f"# Job digest - {started.strftime('%a %b %d, %Y %I:%M %p')} ET", ""]
    L.append(f"Run took {r['seconds']:.0f} s. Local AI: {r['ollama']}. "
             "Nothing was sent: drafts wait for your approval at http://localhost:8765/followups")
    en = r.get("enrich") or {}
    if en and not en.get("skipped"):
        L.append(f"Company info: {en.get('jobs', 0)} jobs enriched in {en.get('seconds', 0):.0f} s "
                 f"({en.get('companies_crawled', 0)} company sites checked, {en.get('companies_cached', 0)} cached, "
                 f"{en.get('anonymous', 0)} anonymous listings skipped); "
                 f"{len(en.get('contacts_set') or [])} lead contact(s) filled from a published hiring email.")
    _ci = lambda j, ind: (md_lines(j["company_info"], ind) if j.get("company_info") else [])  # noqa: E731
    L += ["", "## New jobs this run", ""]
    L.append(f"**{r['new_total']}** new postings (atlanta: {r['new_by_track'].get('atlanta', 0)}, "
             f"remote_ai: {r['new_by_track'].get('remote_ai', 0)}).")
    for t, f in r["fetch"].items():
        if "error" in f:
            L.append(f"- track {t}: FAILED ({f['error']})")
        else:
            L.append(f"- track {t}: {f.get('unique_this_run')} postings seen, {f.get('db_inserted_new')} new rows, "
                     f"{f.get('seconds')} s; DB total {f.get('db_total')}")
    L += ["", f"## Top {len(r['top'])} matches (first seen in the last {cfg['auto']['new_within_hours']} h)", ""]
    for i, j in enumerate(r["top"], 1):
        tag = " **NEW**" if j.get("is_new") else ""
        tag += " (auto-qualified now)" if any(q["id"] == j["id"] for q in r["qualified"]) else ""
        L.append(f"{i}. {_md_job(j)}{tag}")
        if j.get("fit_summary"):
            L.append(f"   - _{j['fit_summary']}_")
        L += _ci(j, "   ")
    if not r["top"]:
        L.append("_No unqualified jobs first seen in this window._")
    L += ["", f"## Newly auto-qualified ({len(r['qualified'])})", ""]
    rule = (f"Rule: score >= {cfg['auto']['min_score']} (resume TF-IDF), fit = direct, not already qualified, "
            f"de-duplicated, max {cfg['auto']['max_qualify_per_run']} per run.")
    L.append(rule)
    L.append("")
    for j in r["qualified"]:
        gens = sorted({d["generator"] for d in j["drafts"]})
        L.append(f"- {_md_job(j)}")
        src = j.get("contact_source") or ""
        src = f" (auto-filled from {src[8:]})" if src.startswith("enrich: ") else ""
        L.append(f"  - contact: {(j.get('contact_email') or 'NEEDS CONTACT EMAIL (add it in the UI)') + src}; drafts by "
                 f"{', '.join(gens)} ({j['draft_seconds']} s)")
        L += _ci(j, "  ")
        for d in j["drafts"]:
            L.append(f"  - touch {d['touch']} ({d['scheduled_for']}, {d['status']} = needs approval): {d['subject']}")
        if j.get("fit_summary"):
            L.append(f"  - _{j['fit_summary']}_")
        if j["drafts"]:
            body = j["drafts"][0]["body"].replace("\n", "\n    ")
            L.append(f"  <details><summary>touch 1 draft</summary>\n\n    {body}\n\n  </details>")
    if not r["qualified"]:
        L.append("_None this run._")
    if r["other_strong"]:
        L += ["", "Also strong but not auto-qualified (per-run cap / per-company cap); qualify in the UI if you like:", ""]
        for j in r["other_strong"]:
            L.append(f"- {_md_job(j)}")
            h = (j.get("company_info") or {}).get("hiring_email")
            if h:
                L.append(f"  - hiring email: {h['email']} - source: {h.get('source_url')}")
    L += ["", f"## Follow-ups due today or overdue ({len(r['due'])})", ""]
    for f in r["due"]:
        to = f["contact_email"] or "NEEDS CONTACT EMAIL"
        L.append(f"- {f['scheduled_for']} touch {f['touch']} [{f['status']}] {f['company']} - {f['title']} -> {to}: {f['subject']}")
    if not r["due"]:
        L.append("_Nothing due._")
    L.append(f"\n{r['upcoming_count']} more drafts scheduled later.")
    L += ["", f"## Blocked: no contact email yet ({len(r['blocked'])})", ""]
    for b in r["blocked"]:
        ci = b.get("company_info") or {}
        L.append(f"- {b['company']} - {b['title']} (next touch {b['next_touch'] or '-'}, {b['qualified_by']}) "
                 f"apply: {ci.get('apply_url') or b['url'] or '-'}")
        h = ci.get("hiring_email")
        if h:
            L.append(f"  - hiring email found (not auto-filled): {h['email']} - source: {h.get('source_url')}")
        elif ci.get("status") == "anonymous":
            L.append("  - anonymous listing (employer name withheld); apply via the board link")
        elif ci.get("website") or ci.get("careers_url"):
            L.append(f"  - {' · '.join(x for x in (ci.get('website'), ci.get('careers_url')) if x)}; no hiring email published")
        if ci.get("general_emails"):
            L.append("  - GENERAL only (not used for follow-ups): " + ", ".join(e["email"] for e in ci["general_emails"][:2]))
    if not r["blocked"]:
        L.append("_None._")
    if r["errors"] or r["source_errors"]:
        L += ["", "## Problems (the run continued past these)", ""]
        L += [f"- {e}" for e in r["errors"]]
        for src, errs in r["source_errors"].items():
            L.append(f"- {src}: {len(errs)} error(s), e.g. {str(errs[0])[:200]}")
    text = "\n".join(L) + "\n"
    path = out_dir / f"digest-{started.strftime('%Y-%m-%d-%H%M')}.md"
    path.write_text(text, encoding="utf-8")
    shutil.copyfile(path, out_dir / "latest-digest.md")
    (out_dir / "auto-last.json").write_text(json.dumps(
        {k: v for k, v in r.items() if k not in ("top", "qualified", "other_strong", "due", "blocked")}, indent=2, default=str),
        encoding="utf-8")
    return str(path)


def publish_dashboard(cfg) -> None:
    """Opt-in (dashboard.publish: true): refresh the phone snapshot on GitHub Pages. Fails soft."""
    if not (cfg.get("dashboard") or {}).get("publish"):
        return
    try:
        from .snapshot import build_and_publish

        res = build_and_publish(cfg)
        print(f"dashboard: {'published ' + (res.get('url') or res.get('branch', '')) if res['ok'] else 'publish FAILED: ' + res['error']}"
              f"  ({res.get('path')})")
    except Exception:  # noqa: BLE001 - never fail the run over the dashboard
        traceback.print_exc()


def main_cli(args, cfg) -> int:
    try:
        r = run_auto(cfg, fetch=not args.no_fetch, qualify=not args.no_qualify)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        publish_dashboard(cfg)
        return 1
    if r.get("skipped"):
        print("skipped: another run holds the lock")
        return 0
    print(f"digest: {r['digest']}  (new {r['new_total']}, qualified {len(r['qualified'])}, errors {len(r['errors'])})")
    publish_dashboard(cfg)
    return 0
