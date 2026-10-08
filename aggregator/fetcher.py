"""Fetch pipeline: sources -> normalize -> track match -> dedupe -> score -> upsert.

Tracks (config `tracks`):
  atlanta   - all job types in the Atlanta metro (search/jobspy/ats/metro settings)
  remote_ai - remote AI-related jobs anywhere in the world (tracks.remote_ai)
Each candidate job carries the set of tracks it matched; the UI filters on it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import Counter, defaultdict

from . import db, followups, llm
from .config import load_companies
from .matching import Filter, TrackMatcher, rescore_all
from .normalize import MetroMatcher, dedupe_hash, job_id, now_iso, remote_region
from .sources import ats as ats_src
from .sources import jobspy_source

log = logging.getLogger("aggregator.fetch")
ATS = ("greenhouse", "lever", "ashby")
TRACKS = ("atlanta", "remote_ai")


def enabled_tracks(cfg: dict, only_tracks: list[str] | None = None) -> list[str]:
    t = [k for k in TRACKS if (cfg.get("tracks", {}).get(k) or {}).get("enabled", k == "atlanta")]
    return [k for k in t if not only_tracks or k in only_tracks]


def run_fetch(cfg: dict, only: list[str] | None = None, only_tracks: list[str] | None = None) -> dict:
    """only: optional subset of sources (e.g. ["greenhouse", "indeed"]); only_tracks: subset of tracks."""
    run_id = uuid.uuid4().hex[:12]
    started = now_iso()
    t0 = time.time()
    conn = db.connect(cfg)
    tracks = enabled_tracks(cfg, only_tracks)
    flt = Filter(cfg)
    metro = MetroMatcher(cfg["metro"])
    include_remote = bool(cfg["search"].get("include_remote"))
    ai = cfg["tracks"]["remote_ai"]
    ai_kw = TrackMatcher(ai.get("keywords") or [])
    ai_min = int(ai.get("description_min_hits") or 0)  # 0 = title match only

    stats = defaultdict(lambda: Counter())       # source -> counters
    track_stats = defaultdict(lambda: Counter())  # track -> source -> kept
    errors: dict[str, list[str]] = defaultdict(list)
    log_rows: list[tuple] = []
    candidates: list[dict] = []
    ok_boards: list[tuple[str, str]] = []

    def want(source):
        return not only or source in only

    def is_ai(job: dict) -> bool:
        return ai_kw.title_match(job.get("title")) or (ai_min > 0 and ai_kw.hits(job.get("description") or "") >= ai_min)

    def add(source: str, job: dict, matched: set[str], locs: list[str] | None = None, board: str | None = None) -> bool:
        if not matched:
            return False
        job["source"] = source
        job["board"] = board
        job["track"] = ",".join(sorted(matched))
        if job.get("remote"):
            job["remote_region"] = remote_region(job.get("location"), *(locs or []), description=job.get("description"))
        stats[source]["kept"] += 1
        for t in matched:
            track_stats[t][source] += 1
        candidates.append(job)
        return True

    def metro_locate(job: dict, locs: list[str] | None) -> bool:
        """In the metro? Also rewrites the display location (which feeds the dedupe hash),
        so it runs for every ATS posting regardless of track -> stable hashes."""
        if "_in_metro" in job:
            return job["_in_metro"]
        in_metro = metro.matches(job.get("location"))
        if not in_metro and locs:
            extra = next((l for l in locs if l and metro.matches(l)), None)
            if extra:  # e.g. Lever allLocations / Ashby secondaryLocations include an Atlanta office
                in_metro = True
                job["location"] = f"{extra} (also {job.get('location')})" if job.get("location") else extra
        job["_in_metro"] = in_metro
        return in_metro

    def atlanta_match(source: str, job: dict, locs: list[str] | None) -> bool:
        in_metro = metro_locate(job, locs)
        if "atlanta" not in tracks:
            return False
        if source in ATS or cfg["jobspy"].get("apply_metro_filter"):
            if not (in_metro or (include_remote and job.get("remote"))):
                return False
        return flt.keep(job)

    # ---- 1. public ATS APIs ------------------------------------------------
    if cfg["ats"].get("enabled"):
        main = load_companies(cfg) if ("atlanta" in tracks or ("remote_ai" in tracks and ai.get("scan_main_companies"))) else {}
        ai_boards = load_companies(cfg, ai["companies_file"]) if "remote_ai" in tracks else {}
        main_slugs = {(a, c["slug"]) for a, lst in main.items() for c in lst}
        ai_slugs = {(a, c["slug"]) for a, lst in ai_boards.items() for c in lst}
        companies: dict[str, list[dict]] = {}
        for src in (main, ai_boards):
            for a, lst in src.items():
                if want(a):
                    seen = {c["slug"] for c in companies.get(a, [])}
                    companies.setdefault(a, []).extend(c for c in lst if c["slug"] not in seen)
        if any(companies.values()):
            log.info("Fetching %d ATS boards for tracks %s ...", sum(len(v) for v in companies.values()), tracks)
            results = asyncio.run(ats_src.fetch_all(companies, cfg["ats"]["concurrency"], cfg["ats"]["timeout_seconds"]))
            for r in results:
                key = (r["ats"], r["slug"])
                kept = 0
                for job, locs in r["jobs"]:
                    stats[r["ats"]]["fetched"] += 1
                    metro_locate(job, locs)
                    m = set()
                    if key in main_slugs and atlanta_match(r["ats"], job, locs):
                        m.add("atlanta")
                    if "remote_ai" in tracks and job.get("remote") and (key in ai_slugs or is_ai(job)):
                        m.add("remote_ai")
                    kept += add(r["ats"], job, m, locs, r["slug"])
                if r["status"] != "error":
                    ok_boards.append(key)
                stats[r["ats"]]["boards_ok" if r["status"] != "error" else "boards_failed"] += 1
                if r["error"]:
                    errors[r["ats"]].append(f"{r['slug']}: {r['error']}")
                stats[f"{r['ats']}:{r['slug']}"].update(fetched=len(r["jobs"]), kept=kept)
                log_rows.append((r["ats"], r["slug"], r["status"], len(r["jobs"]), kept, r["error"]))

    # ---- 2. JobSpy job boards ----------------------------------------------
    def run_jobspy(track: str, plan: dict, match):
        plan["sites"] = [s for s in plan["sites"] if want(s)]
        if not plan["sites"]:
            return
        log.info("JobSpy [%s] sites=%s terms=%d location=%s", track, plan["sites"], len(plan["terms"]), plan["location"])

        def on_result(site, term, status, jobs, err):
            kept = 0
            for j in jobs:
                stats[site]["fetched"] += 1
                kept += add(site, j, {track} if match(site, j) else set())
            stats[site]["queries_" + status] += 1
            if err:
                errors[site].append(f"[{track}] {term!r}: {err}")
            log_rows.append((site, f"[{track}] {term}", status, len(jobs), kept, err))

        jobspy_source.fetch(cfg, on_result, plan)

    if cfg["jobspy"].get("enabled"):
        if "atlanta" in tracks:
            run_jobspy("atlanta", jobspy_source.default_plan(cfg), lambda site, j: atlanta_match(site, j, None))
        if "remote_ai" in tracks:
            aj = ai.get("jobspy") or {}
            plan = {
                "sites": [s for s, on in (aj.get("sites") or {}).items() if on],
                "terms": [t for t in aj.get("search_terms") or [] if t],
                "location": aj.get("locations") or {},
                "distance": 50,  # ignored for country/"Worldwide" searches, but Indeed rejects an empty radius
                "is_remote": True,
                "hours_old": aj.get("hours_old"),
                "results_wanted": aj.get("results_wanted", 25),
                "fetch_description_sites": aj.get("fetch_description_sites"),  # None -> jobspy.fetch_description_sites
            }

            def ai_match(site, j):
                j["remote"] = True  # the query used each site's remote-only filter
                return is_ai(j)
            run_jobspy("remote_ai", plan, ai_match)

    # ---- 3. normalize ids + in-run dedupe -----------------------------------
    fetched_at = now_iso()
    by_hash: dict[str, dict] = {}
    dup_in_run = Counter()
    for j in candidates:
        j["dedupe_hash"] = dedupe_hash(j.get("company"), j.get("title"), j.get("location"))
        j["id"] = job_id(j["source"], j.get("source_job_id"), j.get("url"))
        j["remote"] = 1 if j.get("remote") else 0
        j.setdefault("remote_region", None)
        j["fetched_at"] = j["first_seen_at"] = fetched_at
        prev = by_hash.get(j["dedupe_hash"])
        if prev is None:
            j["seen_on"] = j["source"]
            by_hash[j["dedupe_hash"]] = j
            continue
        if prev is j:
            continue
        dup_in_run[j["source"]] += 1
        # prefer the direct-employer (ATS) copy, else the one with the longer description
        keep, other = (j, prev) if (j["source"] in ATS and prev["source"] not in ATS) else (prev, j)
        if keep is prev and len(j.get("description") or "") > len(prev.get("description") or "") and prev["source"] not in ATS:
            keep, other = j, prev
        seen = set((prev.get("seen_on") or prev["source"]).split(",")) | {j["source"]}
        for k in ("salary_min", "salary_max", "salary_currency", "salary_interval", "posted_at", "job_type", "remote_region",
                  *db.JOBSPY_COLS):
            if keep.get(k) in (None, "Unspecified") and other.get(k) is not None:
                keep[k] = other[k]
        keep["remote"] = max(keep["remote"], other["remote"])
        keep["track"] = ",".join(sorted(set(keep["track"].split(",")) | set(other["track"].split(","))))
        keep["seen_on"] = ",".join(sorted(seen))
        by_hash[j["dedupe_hash"]] = keep
    unique = list(by_hash.values())
    per_track_unique = Counter(t for j in unique for t in j["track"].split(","))
    for j in unique:
        j.pop("_in_metro", None)

    # ---- 4. upsert, prune, score -------------------------------------------
    for j in unique:
        j.setdefault("score", 0.0)
        j.setdefault("llm_json", None)
    inserted, updated = db.upsert_jobs(conn, unique, set(tracks))
    pruned = sum(db.prune_stale_ats(conn, src, slug, fetched_at, set(tracks)) for src, slug in ok_boards)
    rescore_all(cfg, conn)  # resume-profile relevance, with IDF over the whole DB
    for row in log_rows:
        db.log_fetch(conn, run_id, started, *row)
    llm_status = llm.enrich_new_jobs(cfg, conn)
    auto_q = followups.auto_qualify(conn, cfg)  # creates DRAFTS only; never sends

    per_source_unique = Counter(j["source"] for j in unique)
    total_in_db = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    db_by_source = dict(conn.execute("SELECT source, COUNT(*) FROM jobs GROUP BY source").fetchall())
    db_by_track = {t: conn.execute("SELECT COUNT(*) FROM jobs WHERE ',' || track || ',' LIKE ?", (f"%,{t},%",)).fetchone()[0]
                   for t in TRACKS}
    db_regions = dict(conn.execute("SELECT COALESCE(remote_region,'?'), COUNT(*) FROM jobs WHERE ',' || track || ',' LIKE '%,remote_ai,%' "
                                   "GROUP BY 1 ORDER BY 2 DESC").fetchall())
    summary = {
        "run_id": run_id,
        "started_at": started,
        "seconds": round(time.time() - t0, 1),
        "tracks": tracks,
        "location": f"{cfg['search']['location']} +{cfg['search']['distance_miles']}mi",
        "per_source": {
            src: {
                "fetched_raw": c["fetched"],
                "kept_any_track": c["kept"],
                "unique_after_dedupe": per_source_unique.get(src, 0),
                "duplicates_collapsed": dup_in_run.get(src, 0),
                **{k: v for k, v in c.items() if k.startswith(("queries_", "boards_"))},
            }
            for src, c in sorted(stats.items()) if ":" not in src
        },
        "per_track_kept_by_source": {t: dict(c) for t, c in track_stats.items()},
        "per_track_unique": dict(per_track_unique),
        "per_company": {src: dict(c) for src, c in sorted(stats.items()) if ":" in src and c["kept"]},
        "errors": {k: v for k, v in errors.items()},
        "candidates_after_filters": len(candidates),
        "duplicates_collapsed_in_run": sum(dup_in_run.values()),
        "unique_this_run": len(unique),
        "db_inserted_new": inserted,
        "db_updated_existing": updated,
        "db_pruned_stale_ats": pruned,
        "db_total": total_in_db,
        "db_by_source": db_by_source,
        "db_by_track": db_by_track,
        "db_remote_ai_by_region": db_regions,
        "llm": llm_status,
        "auto_qualified": auto_q,
    }
    with conn:
        conn.execute("INSERT INTO runs VALUES (?,?,?)", (run_id, started, json.dumps(summary)))
    conn.close()
    return summary
