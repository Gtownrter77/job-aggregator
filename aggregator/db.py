"""Storage layer.

SQLite for v1. The DDL and upsert use only features shared with PostgreSQL
(TEXT/REAL/INTEGER columns, ISO-8601 timestamps stored as TEXT,
INSERT ... ON CONFLICT (...) DO UPDATE), so moving to Postgres is mostly a
driver swap (sqlite3 -> psycopg) and `?` -> `%s` placeholders.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

from .config import resolve

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,          -- sha1(source + source_job_id or url)
    source          TEXT NOT NULL,             -- indeed|linkedin|google|zip_recruiter|glassdoor|greenhouse|lever|ashby
    source_job_id   TEXT,
    board           TEXT,                      -- ATS board slug (greenhouse/lever/ashby)
    company         TEXT,
    title           TEXT NOT NULL,
    location        TEXT,
    remote          INTEGER NOT NULL DEFAULT 0, -- 0/1 (BOOLEAN in Postgres)
    salary_min      REAL,
    salary_max      REAL,
    salary_currency TEXT,
    salary_interval TEXT,
    job_type        TEXT,
    url             TEXT,
    description     TEXT,
    posted_at       TEXT,                      -- ISO-8601 date/datetime (TIMESTAMPTZ in Postgres)
    fetched_at      TEXT NOT NULL,             -- last time we saw it
    first_seen_at   TEXT NOT NULL,
    seen_on         TEXT,                      -- comma list of every source that returned this posting
    score           REAL NOT NULL DEFAULT 0,   -- relevance vs config scoring.profile
    llm_json        TEXT,                      -- optional structured extraction (Ollama hook)
    dedupe_hash     TEXT NOT NULL UNIQUE,      -- sha1(normalized company|title|location)
    track           TEXT,                      -- comma list of search tracks it matched: atlanta,remote_ai
    remote_region   TEXT                       -- remote jobs: US | Worldwide | Europe | UK | ... | Unspecified
);
CREATE INDEX IF NOT EXISTS idx_jobs_posted ON jobs(posted_at);
CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs(source);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);
CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(score);

CREATE TABLE IF NOT EXISTS fetch_log (
    run_id      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    source      TEXT NOT NULL,      -- e.g. indeed, greenhouse
    target      TEXT,               -- search term or company slug
    status      TEXT NOT NULL,      -- ok|empty|error|skipped
    fetched     INTEGER NOT NULL DEFAULT 0,
    kept        INTEGER NOT NULL DEFAULT 0,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_fetch_log_run ON fetch_log(run_id);

-- A job the user marked "qualified" (or auto-qualified by score) + its contact.
CREATE TABLE IF NOT EXISTS leads (
    job_id          TEXT PRIMARY KEY REFERENCES jobs(id),
    qualified_at    TEXT NOT NULL,             -- ISO date the sequence is anchored to
    qualified_by    TEXT NOT NULL,             -- manual|auto
    contact_name    TEXT,
    contact_email   TEXT,                      -- NULL => "needs contact"
    contact_source  TEXT,                      -- manual|posting
    status          TEXT NOT NULL DEFAULT 'active',  -- active|replied|closed
    replied_at      TEXT,
    updated_at      TEXT NOT NULL
);

-- 3-touch follow-up drafts. Nothing here is ever sent unless status='approved'
-- AND someone runs `python -m aggregator send-approved` with SMTP configured.
CREATE TABLE IF NOT EXISTS followups (
    id            TEXT PRIMARY KEY,            -- job_id + ':' + touch
    job_id        TEXT NOT NULL REFERENCES jobs(id),
    touch         INTEGER NOT NULL CHECK (touch BETWEEN 1 AND 3),
    scheduled_for TEXT NOT NULL,               -- ISO date (local)
    status        TEXT NOT NULL DEFAULT 'draft'
                  CHECK (status IN ('draft','approved','sent','skipped','replied')),
    subject       TEXT NOT NULL,
    body          TEXT NOT NULL,
    edited        INTEGER NOT NULL DEFAULT 0,  -- 1 = hand-edited, don't re-render
    generator     TEXT,                        -- templates|llm
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    approved_at   TEXT,
    sent_at       TEXT,
    send_error    TEXT,
    UNIQUE (job_id, touch)
);
CREATE INDEX IF NOT EXISTS idx_followups_due ON followups(status, scheduled_for);

CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    summary_json TEXT
);
"""

JOB_COLS = [
    "id", "source", "source_job_id", "board", "company", "title", "location", "remote",
    "salary_min", "salary_max", "salary_currency", "salary_interval", "job_type",
    "url", "description", "posted_at", "fetched_at", "first_seen_at", "seen_on",
    "score", "llm_json", "dedupe_hash", "track", "remote_region",
]

# On a dedupe-hash collision keep the original row (and its id/source) but
# refresh volatile fields and fill gaps. ATS (direct employer) data wins for
# url/description because it is the canonical posting.
UPSERT_SQL = f"""
INSERT INTO jobs ({", ".join(JOB_COLS)})
VALUES ({", ".join("?" for _ in JOB_COLS)})
ON CONFLICT (dedupe_hash) DO UPDATE SET
    fetched_at      = excluded.fetched_at,
    track           = excluded.track,        -- merged in Python (see upsert_jobs)
    remote_region   = CASE WHEN excluded.remote_region IS NOT NULL AND excluded.remote_region <> 'Unspecified'
                           THEN excluded.remote_region ELSE COALESCE(jobs.remote_region, excluded.remote_region) END,
    board           = COALESCE(jobs.board, excluded.board),
    remote          = CASE WHEN excluded.remote = 1 THEN 1 ELSE jobs.remote END,
    salary_min      = COALESCE(jobs.salary_min, excluded.salary_min),
    salary_max      = COALESCE(jobs.salary_max, excluded.salary_max),
    salary_currency = COALESCE(jobs.salary_currency, excluded.salary_currency),
    salary_interval = COALESCE(jobs.salary_interval, excluded.salary_interval),
    job_type        = COALESCE(jobs.job_type, excluded.job_type),
    posted_at       = COALESCE(jobs.posted_at, excluded.posted_at),
    url = CASE WHEN excluded.source IN ('greenhouse','lever','ashby') AND jobs.source NOT IN ('greenhouse','lever','ashby')
               THEN excluded.url ELSE COALESCE(jobs.url, excluded.url) END,
    description = CASE
        WHEN jobs.description IS NULL OR length(jobs.description) < length(COALESCE(excluded.description, ''))
        THEN excluded.description ELSE jobs.description END,
    seen_on = CASE
        WHEN (',' || jobs.seen_on || ',') LIKE ('%,' || excluded.source || ',%') THEN jobs.seen_on
        ELSE jobs.seen_on || ',' || excluded.source END
"""


def connect(cfg: dict) -> sqlite3.Connection:
    path = resolve(cfg["database"]["path"])
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn):
    """Additive column migrations for DBs created by older versions."""
    wanted = {"followups": {"generator": "TEXT"},
              "jobs": {"board": "TEXT", "track": "TEXT", "remote_region": "TEXT"}}
    for table, cols in wanted.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
                if (table, col) == ("jobs", "track"):  # everything before tracks existed was the Atlanta search
                    conn.execute("UPDATE jobs SET track = 'atlanta'")
                if (table, col) == ("jobs", "remote_region"):
                    from .normalize import remote_region
                    rows = conn.execute("SELECT id, location, description FROM jobs WHERE remote = 1").fetchall()
                    conn.executemany("UPDATE jobs SET remote_region = ? WHERE id = ?",
                                     [(remote_region(r[1], description=r[2]), r[0]) for r in rows])
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_track ON jobs(track)")
    conn.commit()


def _tracks(s: str | None) -> set[str]:
    return {t for t in (s or "").split(",") if t}


def upsert_jobs(conn: sqlite3.Connection, jobs: Iterable[dict], tracks_run: set[str] | None = None,
                authoritative: Iterable[str] = ("greenhouse", "lever", "ashby")) -> tuple[int, int]:
    """Upsert jobs. Returns (inserted_new, updated_existing).

    Track tags: for ATS postings (a full board is read each run) the tracks evaluated in
    this run are authoritative: new = (old - tracks_run) | matched. For JobSpy (search
    results are a sample) tags only accumulate: new = old | matched."""
    inserted = updated = 0
    tracks_run = set(tracks_run or ())
    with conn:
        for j in jobs:
            # Same posting (same source id) whose title/location/company changed -> re-key it.
            old = conn.execute("SELECT dedupe_hash FROM jobs WHERE id = ?", (j["id"],)).fetchone()
            if old and old[0] != j["dedupe_hash"]:
                taken = conn.execute("SELECT 1 FROM jobs WHERE dedupe_hash = ?", (j["dedupe_hash"],)).fetchone()
                if taken:
                    conn.execute("DELETE FROM jobs WHERE id = ? AND id NOT IN (SELECT job_id FROM leads)", (j["id"],))
                    if conn.execute("SELECT 1 FROM jobs WHERE id = ?", (j["id"],)).fetchone():
                        continue  # qualified lead: keep as-is
                else:
                    conn.execute("UPDATE jobs SET dedupe_hash = ?, title = ?, company = ?, location = ? WHERE id = ?",
                                 (j["dedupe_hash"], j["title"], j["company"], j["location"], j["id"]))
            exists = conn.execute("SELECT track FROM jobs WHERE dedupe_hash = ?", (j["dedupe_hash"],)).fetchone()
            if exists is not None:
                old_t = _tracks(exists[0])
                keep_old = old_t - tracks_run if j["source"] in authoritative else old_t
                j["track"] = ",".join(sorted(keep_old | _tracks(j.get("track"))))
            conn.execute(UPSERT_SQL, [j.get(c) for c in JOB_COLS])
            if exists:
                updated += 1
            else:
                inserted += 1
    return inserted, updated


def prune_stale_ats(conn, source: str, board: str, fetched_at: str, tracks_run: set[str] | None = None) -> int:
    """A board fetched OK is authoritative: its postings not seen this run are closed or no
    longer match. The tracks evaluated this run are removed from them; rows left with no
    track are deleted. Qualified leads are kept. Returns rows deleted."""
    tracks_run = set(tracks_run or ())
    deleted = 0
    with conn:
        rows = conn.execute("SELECT id, track FROM jobs WHERE source = ? AND board = ? AND fetched_at < ?",
                            (source, board, fetched_at)).fetchall()
        leads = {r[0] for r in conn.execute("SELECT job_id FROM leads")}
        for jid, tr in rows:
            left = _tracks(tr) - tracks_run if tracks_run else set()
            if left:
                conn.execute("UPDATE jobs SET track = ? WHERE id = ?", (",".join(sorted(left)), jid))
            elif jid not in leads:
                conn.execute("DELETE FROM jobs WHERE id = ?", (jid,))
                deleted += 1
    return deleted


def log_fetch(conn, run_id, started_at, source, target, status, fetched=0, kept=0, error=None):
    with conn:
        conn.execute(
            "INSERT INTO fetch_log (run_id, started_at, source, target, status, fetched, kept, error) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, started_at, source, target, status, fetched, kept, (error or "")[:500] or None),
        )
