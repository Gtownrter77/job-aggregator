"""Filtering (roles / exclude / remote / metro) and relevance scoring."""
from __future__ import annotations

import logging
import re

log = logging.getLogger("aggregator.matching")


class Filter:
    def __init__(self, cfg: dict):
        s = cfg["search"]
        self.roles = [r.lower() for r in s.get("roles") or [] if r]
        self.exclude = [x.lower() for x in s.get("exclude_keywords") or [] if x]
        self.remote_only = bool(s.get("remote_only"))
        self.include_remote = bool(s.get("include_remote"))

    def keep(self, job: dict) -> bool:
        title = (job.get("title") or "").lower()
        if not title:
            return False
        if self.roles and not any(r in title for r in self.roles):
            return False
        if self.exclude and any(re.search(rf"\b{re.escape(x)}\b", title) for x in self.exclude):
            return False
        if self.remote_only and not job.get("remote"):
            return False
        return True


def _doc(job: dict) -> str:
    # title weighted 3x so it dominates long boilerplate descriptions
    return f"{job.get('title','')} " * 3 + f"{job.get('company','')} {(job.get('description') or '')[:4000]}"


def tfidf_scores(query: str, docs: list[str]) -> list[float]:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import linear_kernel

    if not query.strip() or not docs:
        return [0.0] * len(docs)
    vec = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), sublinear_tf=True, min_df=1, max_features=200_000)
    try:
        m = vec.fit_transform(docs + [query])
    except ValueError:  # empty vocabulary
        return [0.0] * len(docs)
    return [float(x) for x in linear_kernel(m[-1], m[:-1]).ravel()]


def embedding_scores(query: str, docs: list[str], model_name: str) -> list[float] | None:
    try:
        from sentence_transformers import SentenceTransformer, util  # optional dependency
    except ImportError:
        return None
    model = SentenceTransformer(model_name, device="cpu")
    q = model.encode([query], normalize_embeddings=True)
    d = model.encode(docs, normalize_embeddings=True, batch_size=64)
    return [float(x) for x in util.cos_sim(q, d)[0]]


def profile_text(cfg: dict) -> str:
    """What jobs are scored against: scoring.profile + search.roles + the resume-derived
    text file (scoring.resume_file, built by scripts/build_profile.py from your resumes)."""
    from pathlib import Path

    from .config import resolve

    sc = cfg["scoring"]
    parts = [sc.get("profile") or ""] + list(cfg["search"].get("roles") or [])
    rf = sc.get("resume_file")
    if rf and Path(resolve(rf)).exists():
        parts.append(Path(resolve(rf)).read_text(encoding="utf-8", errors="replace"))
    return " ".join(parts).strip()


class TrackMatcher:
    """Keyword test for a track such as remote_ai (word-boundary, case-insensitive;
    short all-caps keywords like AI / ML / LLM are matched case-sensitively)."""

    def __init__(self, keywords: list[str]):
        ci = [k for k in keywords if not (k.isupper() and len(k) <= 4)]
        cs = [k for k in keywords if k.isupper() and len(k) <= 4]
        self.ci = re.compile(r"\b(" + "|".join(re.escape(k) for k in ci) + r")\b", re.I) if ci else None
        self.cs = re.compile(r"\b(" + "|".join(re.escape(k) for k in cs) + r")s?\b") if cs else None

    def hits(self, text: str) -> int:
        text = text or ""
        return sum(len(rx.findall(text)) for rx in (self.ci, self.cs) if rx)

    def title_match(self, title: str) -> bool:
        return self.hits(title) > 0


def score_jobs(cfg: dict, jobs: list[dict]) -> list[float]:
    """Relevance of each job vs the resume profile (+ scoring.profile, roles). 0..1. All zeros if no profile."""
    sc = cfg["scoring"]
    profile = profile_text(cfg)
    if not profile:
        return [0.0] * len(jobs)
    docs = [_doc(j) for j in jobs]
    if sc.get("method") == "embeddings":
        out = embedding_scores(profile, docs, sc["embedding_model"])
        if out is not None:
            return out
        log.warning("sentence-transformers not installed; falling back to TF-IDF")
    return tfidf_scores(profile, docs)


def rescore_all(cfg: dict, conn) -> int:
    """Recompute the stored score for every job (profile may have changed)."""
    rows = conn.execute("SELECT id, title, company, description FROM jobs").fetchall()
    jobs = [dict(r) for r in rows]
    scores = score_jobs(cfg, jobs)
    with conn:
        conn.executemany("UPDATE jobs SET score = ? WHERE id = ?", [(round(s, 5), j["id"]) for s, j in zip(scores, jobs)])
    return len(jobs)
