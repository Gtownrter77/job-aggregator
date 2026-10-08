from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict[str, Any] = {
    "search": {
        "location": "Atlanta, GA",
        "distance_miles": 50,
        "roles": [],
        "exclude_keywords": [],
        "remote_only": False,
        "include_remote": False,
        "hours_old": 168,
    },
    "jobspy": {
        "enabled": True,
        "sites": {"indeed": True, "linkedin": True, "google": True, "zip_recruiter": True, "glassdoor": True},
        "search_terms": ["full time", "manager", "sales"],
        "results_wanted": 40,
        "country_indeed": "usa",
        "linkedin_fetch_description": False,
        "max_consecutive_failures": 2,
        "apply_metro_filter": False,
        "proxies": [],
        "fetch_description_sites": ["glassdoor"],
    },
    "ats": {"enabled": True, "companies_file": "companies.yaml", "concurrency": 8, "timeout_seconds": 45},
    "metro": {"name": "", "towns": [], "towns_require_state": [], "state_tokens": ["GA", "Georgia"]},
    "tracks": {
        "atlanta": {"enabled": True},
        "remote_ai": {
            "enabled": False,
            "remote_only": True,
            "companies_file": "companies_ai.yaml",
            "scan_main_companies": True,
            "keywords": ["AI", "ML", "LLM", "machine learning", "artificial intelligence"],
            "description_min_hits": 3,
            "jobspy": {"sites": {"indeed": True, "linkedin": True}, "search_terms": ["AI"], "locations": {},
                       "results_wanted": 25, "hours_old": 168},
        },
    },
    "scoring": {"method": "tfidf", "profile": "", "resume_file": "", "embedding_model": "sentence-transformers/all-MiniLM-L6-v2"},
    "llm": {"enabled": False, "ollama_url": "http://localhost:11434", "model": "llama3.2:3b", "max_jobs_per_run": 50,
            "num_ctx": 4096, "keep_alive": "15m", "timeout_seconds": 240, "draft_attempts": 3, "fit_summary": True,
            "extract_on_fetch": False},
    "auto": {"min_score": 0.04, "require_direct_fit": True, "max_qualify_per_run": 5, "max_per_company_per_run": 2,
             "new_within_hours": 72, "exclude_title_regex": r"\b(intern|internship|co-?op|college grads?|new grads?|student|apprentice)\b",
             "top_n": 5, "digest_dir": "logs"},
    "enrich": {"enabled": True, "cache_days": 30, "max_seconds_per_run": 270, "max_companies_per_run": 25,
               "max_pages_per_company": 8, "request_timeout_seconds": 10, "delay_seconds": 0.6, "search": True,
               "fetch_board_details": True, "auto_set_contact": True, "top_jobs": 20,
               "user_agent": "Mozilla/5.0 (compatible; job-aggregator-enrich/1.0; +https://github.com/Gtownrter77/job-aggregator)"},
    "database": {"path": "data/jobs.db"},
    "server": {"host": "0.0.0.0", "port": 8765},
    "followups": {
        "applicant_name": "",
        "applicant_phone": "",
        "applicant_email": "",
        "default_contact_name": "Hiring Team",
        "schedule_days": [0, 3, 10],
        "skip_weekends": True,
        "templates_file": "followup_templates.yaml",
        "auto_qualify_score": None,
        "contact_from_posting": True,
        "sending_enabled": False,
        "use_llm": True,
    },
    "applicant": {"headline": "", "skills": [], "accomplishments": [], "profile_file": "",
                  "location_fact": "", "home_base": "", "work_area": "", "home_area_places": []},
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def resolve(path: str | os.PathLike) -> Path:
    p = Path(path)
    return p if p.is_absolute() else ROOT / p


def _local_path(p: Path) -> Path:
    """Personal overrides live next to the config: config.yaml -> config.local.yaml
    (gitignored). $AGGREGATOR_LOCAL_CONFIG points somewhere else."""
    env = os.environ.get("AGGREGATOR_LOCAL_CONFIG")
    if env:
        return resolve(env)
    return p.with_name(p.stem + ".local" + p.suffix)


def load_config(path: str | None = None) -> dict:
    path = path or os.environ.get("AGGREGATOR_CONFIG") or str(ROOT / "config.yaml")
    p = resolve(path)
    data = {}
    if p.exists():
        data = yaml.safe_load(p.read_text()) or {}
    cfg = _merge(DEFAULTS, data)
    # Private, never-committed overrides (your name/phone/email, resume paths, ...).
    local = _local_path(p)
    if local.exists():
        cfg = _merge(cfg, yaml.safe_load(local.read_text()) or {})
        cfg["_local_path"] = str(local)
    # metro.extra_towns: appended to metro.towns (handy in config.local.yaml).
    metro = cfg.get("metro") or {}
    extra = [t for t in (metro.get("extra_towns") or []) if t and t not in (metro.get("towns") or [])]
    if extra:
        metro["towns"] = list(metro.get("towns") or []) + extra
    cfg["_path"] = str(p)
    return cfg


def load_companies(cfg: dict, path: str | None = None) -> dict[str, list[dict]]:
    p = resolve(path or cfg["ats"]["companies_file"])
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text()) or {}
    out: dict[str, list[dict]] = {}
    for ats in ("greenhouse", "lever", "ashby"):
        out[ats] = [c if isinstance(c, dict) else {"slug": str(c)} for c in (data.get(ats) or [])]
    return out
