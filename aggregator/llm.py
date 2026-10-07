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


def ollama_available(cfg: dict, model: bool = False) -> bool:
    """Ollama answers (and, with model=True, has llm.model pulled)."""
    try:
        r = httpx.get(cfg["llm"]["ollama_url"].rstrip("/") + "/api/tags", timeout=3)
        if r.status_code != 200:
            return False
        if not model:
            return True
        want = cfg["llm"]["model"]
        names = {m.get("name") for m in r.json().get("models") or []}
        return want in names or f"{want}:latest" in names
    except Exception:
        return False


def generate(cfg: dict, prompt: str, *, system: str | None = None, schema: dict | None = None,
             temperature: float = 0.7, num_predict: int = 400, seed: int | None = None,
             timeout: float | None = None) -> tuple[str, dict]:
    """One non-streaming completion from the local Ollama model.
    Returns (text, stats). Raises on HTTP/connection errors (callers fall back)."""
    llm = cfg["llm"]
    options = {"temperature": temperature, "num_predict": num_predict, "num_ctx": int(llm.get("num_ctx") or 4096),
               "top_p": 0.9, "repeat_penalty": 1.1}
    if seed is not None:
        options["seed"] = seed
    body = {"model": llm["model"], "prompt": prompt, "stream": False, "options": options,
            "keep_alive": llm.get("keep_alive", "15m")}
    if system:
        body["system"] = system
    if schema:
        body["format"] = schema
    r = httpx.post(llm["ollama_url"].rstrip("/") + "/api/generate", json=body,
                   timeout=timeout or float(llm.get("timeout_seconds") or 240))
    r.raise_for_status()
    d = r.json()
    stats = {"seconds": round((d.get("total_duration") or 0) / 1e9, 1), "prompt_tokens": d.get("prompt_eval_count"),
             "output_tokens": d.get("eval_count")}
    return (d.get("response") or "").strip(), stats


FIT_PROMPT = """Compare the job posting with the applicant facts. Use ONLY these facts; never invent credentials,
degrees, certifications, numbers or experience.

APPLICANT FACTS:
{facts}

JOB: {title} at {company} ({location})
POSTING EXCERPT:
{excerpt}

Reply with exactly two lines and nothing else:
Fit: <one sentence, at most 20 words, naming the specific applicant fact that matches what the posting asks for>
Gap: <one sentence, at most 15 words: a specific requirement from the posting (a license, software, degree, industry,
years, travel, office/admin duty...) that the facts do NOT mention; write "none obvious" only if every requirement is covered>"""


def fit_summary(cfg: dict, job: dict, facts: str) -> str | None:
    """'Fit: ... Gap: ...' note for the digest (local model; None on failure)."""
    import re

    try:
        text, _ = generate(cfg, FIT_PROMPT.format(facts=facts, title=job.get("title") or "", company=job.get("company") or "",
                                                  location=job.get("location") or "",
                                                  excerpt=(job.get("description") or "")[:1800]),
                           temperature=0.2, num_predict=90, seed=7, timeout=120)
        fit = re.search(r"fit\s*:\s*(.+)", text, re.I)
        gap = re.search(r"gap\s*:\s*(.+)", text, re.I)
        if not fit:
            return None
        out = "Fit: " + fit.group(1).strip()
        if gap:
            out += " Gap: " + gap.group(1).strip()
        return out[:300]
    except Exception as e:  # noqa: BLE001
        log.warning("fit summary failed for %s: %s", job.get("id"), e)
        return None


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
    if not cfg["llm"].get("extract_on_fetch", True):
        return "per-job extraction off (llm.extract_on_fetch: false); model used for drafts/fit summaries"
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
