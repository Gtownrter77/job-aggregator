"""Optional LLM extraction hook (local Ollama only; no paid APIs).

When `llm.enabled: true` in config.yaml AND an Ollama server answers at
`llm.ollama_url`, each new job's description is sent to the local model,
which returns structured JSON stored in jobs.llm_json. Otherwise this is a
no-op. Swap `extract()` for any other local model runner if you prefer.
"""
from __future__ import annotations

import json
import logging

import httpx

log = logging.getLogger("aggregator.llm")

PROMPT = """Extract facts from this job posting. Reply with ONLY a JSON object with keys:
seniority (intern|junior|mid|senior|lead|manager|director|executive|unknown),
employment_type (full_time|part_time|contract|temporary|internship|unknown),
salary_min (number|null), salary_max (number|null), salary_currency (string|null),
remote (true|false), skills (list of up to 10 short strings), summary (one sentence).

TITLE: {title}
COMPANY: {company}
LOCATION: {location}
DESCRIPTION:
{description}
"""


def ollama_available(cfg: dict) -> bool:
    try:
        r = httpx.get(cfg["llm"]["ollama_url"].rstrip("/") + "/api/tags", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def extract(cfg: dict, job: dict) -> dict | None:
    """Return structured fields for one job, or None on any failure."""
    try:
        r = httpx.post(
            cfg["llm"]["ollama_url"].rstrip("/") + "/api/generate",
            json={
                "model": cfg["llm"]["model"],
                "prompt": PROMPT.format(**{k: job.get(k) or "" for k in ("title", "company", "location")},
                                        description=(job.get("description") or "")[:6000]),
                "format": "json",
                "stream": False,
            },
            timeout=120,
        )
        r.raise_for_status()
        return json.loads(r.json().get("response") or "{}")
    except Exception as e:  # noqa: BLE001
        log.warning("LLM extraction failed for %s: %s", job.get("id"), e)
        return None


def enrich_new_jobs(cfg: dict, conn) -> str:
    if not cfg["llm"].get("enabled"):
        return "disabled in config (llm.enabled: false)"
    if not ollama_available(cfg):
        return f"skipped: Ollama not reachable at {cfg['llm']['ollama_url']}"
    rows = conn.execute(
        "SELECT id, title, company, location, description FROM jobs WHERE llm_json IS NULL ORDER BY fetched_at DESC LIMIT ?",
        (cfg["llm"]["max_jobs_per_run"],),
    ).fetchall()
    n = 0
    for row in rows:
        data = extract(cfg, dict(row))
        if data is not None:
            with conn:
                conn.execute("UPDATE jobs SET llm_json = ? WHERE id = ?", (json.dumps(data), row["id"]))
            n += 1
    return f"enriched {n}/{len(rows)} jobs with {cfg['llm']['model']}"
