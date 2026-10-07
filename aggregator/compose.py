"""Per-job follow-up copy: posting-detail extraction + rich rotating templates,
with an optional local-LLM (Ollama) writer. Produces DRAFTS only."""
from __future__ import annotations

import hashlib
import json
import logging
import re

import httpx
import yaml

from .config import resolve
from .normalize import MetroMatcher

log = logging.getLogger("aggregator.compose")
MARKER_RE = re.compile(r"\[\[.*?\]\]", re.S)

# Recognizable skills/themes across many job families -> display phrase.
SKILLS = {
    r"\bpython\b": "Python", r"\bsql\b": "SQL", r"\bexcel\b": "Excel", r"\bsalesforce\b": "Salesforce",
    r"\baws\b": "AWS", r"\bkubernetes\b": "Kubernetes", r"\breact\b": "React", r"\bjava\b": "Java",
    r"\bmachine learning\b": "machine learning", r"\bdata analy": "data analysis", r"\btableau\b": "Tableau",
    r"\bpower ?bi\b": "Power BI", r"\bproject management\b": "project management", r"\bagile\b": "agile delivery",
    r"\bcustomer service\b": "customer service", r"\bcustomer experience\b": "customer experience",
    r"\bpatient care\b": "patient care", r"\bpatient[- ]centered\b": "patient-centered care", r"\bepic\b": "Epic",
    r"\bforklift\b": "forklift operations", r"\bcdl\b": "CDL driving", r"\bhvac\b": "HVAC", r"\bosha\b": "OSHA safety",
    r"\bscheduling\b": "scheduling", r"\binventory\b": "inventory management", r"\blogistics\b": "logistics",
    r"\bsupply chain\b": "supply chain", r"\bquickbooks\b": "QuickBooks", r"\baccounts payable\b": "accounts payable",
    r"\bpayroll\b": "payroll", r"\bcompliance\b": "compliance", r"\bb2b\b": "B2B sales", r"\bcrm\b": "CRM",
    r"\bnegotiat": "negotiation", r"\bprospect": "prospecting", r"\bcoaching\b": "coaching", r"\bmentor": "mentoring",
    r"\bbudget": "budgeting", r"\bforecast": "forecasting", r"\bmarketing\b": "marketing", r"\bseo\b": "SEO",
    r"\bsocial media\b": "social media", r"\bcontent\b": "content", r"\bux\b": "UX", r"\bfigma\b": "Figma",
    r"\bsecurity\b": "security", r"\bcloud\b": "cloud infrastructure", r"\bapi\b": "APIs",
    r"\bquality assurance\b|\bqa\b": "quality assurance", r"\bmaintenance\b": "maintenance",
    r"\bfood safety\b": "food safety", r"\bhospitality\b": "hospitality", r"\bguest\b": "guest experience",
    r"\btraining\b": "training", r"\bteam leadership\b|\blead a team\b|\bleading teams\b": "team leadership",
    r"\bstakeholder": "stakeholder management", r"\bprocess improvement\b": "process improvement",
    r"\bsix sigma\b|\blean\b": "lean/Six Sigma", r"\bmedicaid\b|\bmedicare\b": "Medicare/Medicaid",
    r"\bunderwriting\b": "underwriting", r"\bclaims\b": "claims", r"\bconstruction\b": "construction",
    r"\belectrical\b": "electrical work", r"\bplumbing\b": "plumbing", r"\bwelding\b": "welding",
}
_HOOK_WORDS = re.compile(r"\b(mission|we're building|we are building|we believe|our goal|our purpose|our vision|help(?:s|ing)? (?:families|people|customers|businesses|patients|teams|companies|communities|organizations)|empower(?:s|ing)?|on a mission|transform(?:s|ing)?|reimagin(?:e|ing)|we serve|we build|we make)\b", re.I)


def _seed(*parts) -> int:
    return int(hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:8], 16)


def _pick(lst, *seed):
    return lst[_seed(*seed) % len(lst)] if lst else ""


def clean_role(title: str) -> str:
    t = re.sub(r"\s*\(.*?\)\s*", " ", title or "")
    t = re.split(r"\s+[-–|]\s+(?=[A-Z][a-z]+,?\s*(?:GA|Georgia)\b|Atlanta|Remote|Hybrid|Onsite|On-site|FT|PT|Full|Part)", t)[0]
    t = re.sub(r"\s+", " ", t).strip(" -–|,")
    return t[:70] or "this"


_HOOK_BAD = re.compile(r"https?://|@|equal opportunity|benefit|salary|\$|EEO|accommodat|apply|click|resume|background check|drug|years of experience|degree|requirements?\b|qualifications?|we are looking for|we're looking for|you will\b|you'll\b|responsibilities|candidate|AI tools|diversity|inclusion|backgrounds|discriminat|hiring", re.I)


def _hook_phrase(desc: str, company: str = "") -> str | None:
    """A short, quotable, complete sentence from the posting (mission / what the team does)."""
    text = re.sub(r"[*_#>`]|\\|®|™", "", desc or "")
    for s in re.split(r"(?<=[.!])\s+|\n+", text)[:80]:
        s = s.strip(" -•\t")
        if not s or not s[0].isupper() or not s.endswith((".", "!")):
            continue  # fragments / list headers
        if not _HOOK_WORDS.search(s) or _HOOK_BAD.search(s):
            continue
        words = s.split()
        if not 7 <= len(words) <= 22:
            continue
        if company and re.match(rf"{re.escape(company.split()[0])}\b", s, re.I):
            continue  # "Acme is the leading..." = company boilerplate, not about the role
        s = s.rstrip(".!")
        # "At Acme, we help families..." -> "we help families..."
        s = re.sub(r"^(At|Here at)\s+[^,]{2,40},\s*", "", s)
        first = s.split()[0].strip(",;:")
        if first in ("This", "It", "These", "They", "That", "Those") or first.lower().endswith("ing"):
            continue  # vague referent or a duties bullet
        if len(s) > 25:
            return s[0].lower() + s[1:] if s.split()[0] in ("We", "Our", "Is", "Helps") else s
    return None


_CO_SUFFIX = re.compile(r"[,\s]+(inc\.?|llc|l\.l\.c\.|ltd\.?|limited|corp\.?|corporation|co\.|plc|lp|llp)\s*$", re.I)


def display_company(name: str | None) -> str:
    n = re.sub(r"[®™]", "", name or "").strip()
    for _ in range(2):
        n = _CO_SUFFIX.sub("", n).strip()
    return n or "your team"


_GENERIC = {"security", "maintenance", "training", "compliance", "marketing", "content", "budgeting",
            "prospecting", "cloud infrastructure", "APIs", "scheduling", "claims", "customer service", "forecasting"}


def posting_skills(text: str, title: str = "", limit: int = 3) -> list[str]:
    """Most prominent recognizable skills; title hits weigh most, generic words need repetition."""
    scored = {}
    for pat, name in SKILLS.items():
        n = len(re.findall(pat, text or "", re.I))
        in_title = bool(re.search(pat, title or "", re.I))
        if not n and not in_title:
            continue
        if name in _GENERIC and n < 3 and not in_title:
            continue
        scored[name] = max(scored.get(name, 0), n + (10 if in_title else 0))
    return [k for k, _ in sorted(scored.items(), key=lambda kv: -kv[1])][:limit]


def details(job: dict, cfg: dict) -> dict:
    loc = job.get("location") or ""
    city = (loc.split(",")[0] or "").strip() or cfg["search"]["location"].split(",")[0]
    if re.search(r"remote", city, re.I):
        city = cfg["search"]["location"].split(",")[0]
    desc = job.get("description") or ""
    skills = posting_skills(desc, job.get("title") or "")
    company = display_company(job.get("company"))
    hook = _hook_phrase(desc, company)
    role = clean_role(job.get("title") or "")
    options = []
    if hook:
        options.append(f'the line in your posting about "{hook}"')
    if len(skills) >= 2:
        options.append(f"the posting's emphasis on {skills[0]} and {skills[1]}")
    elif skills:
        options.append(f"the focus on {skills[0]} in the posting")
    in_metro = _metro(cfg).matches(loc)
    if not options:
        options.append(f"the chance to do this work with {company} right here in {city}" if in_metro
                       else f"the chance to do this work with {company}")
    detail = options[0]  # the posting's own words beat a skills summary
    # touch 3 refers back briefly instead of repeating a long quote
    later = options[1] if (hook and len(options) > 1) else (f"the {role} work" if hook else detail)
    return {"role": role, "city": city, "skills": skills, "hook": hook, "detail": detail, "detail_later": later,
            "in_metro": in_metro, "company": company}


_PROFILE_CACHE: dict = {}


def _profile_accomplishments(path: str) -> list[dict]:
    """Corroborated accomplishments from resumes/profile.json (items with a `use` sentence
    and no `confirm` flag). Low-confidence claims are never used in drafts."""
    p = resolve(path)
    if not p.exists():
        return []
    key = (str(p), p.stat().st_mtime)
    if key not in _PROFILE_CACHE:
        data = json.loads(p.read_text())
        _PROFILE_CACHE.clear()
        _PROFILE_CACHE[key] = [
            {"text": a["use"], "tags": set((a.get("tags") or "").lower().split()), "versions": a.get("versions", 0)}
            for a in data.get("accomplishments") or [] if a.get("use") and not a.get("confirm")
        ]
    return _PROFILE_CACHE[key]


def _applicant(cfg):
    a = cfg.get("applicant") or {}
    fu = cfg["followups"]
    name = (fu.get("applicant_name") or a.get("name") or "").strip()
    sig = [name or "[[your name]]"] + [x.strip() for x in (fu.get("applicant_phone"), fu.get("applicant_email")) if x and x.strip()]
    accs = [{"text": x.strip().rstrip("."), "tags": set(), "versions": 0} for x in a.get("accomplishments") or [] if x]
    if a.get("profile_file"):
        accs += _profile_accomplishments(a["profile_file"])
    return {
        "name": name or "[[your name]]",
        "signature": "\n".join(sig),
        "first": (name.split()[0] if name else "[[your first name]]"),
        "headline": (a.get("headline") or "").strip(),
        "skills": [s for s in a.get("skills") or [] if s],
        "accomplishments": accs,
    }


# Titles where the resume is a direct fit (construction/field/sales/service work).
_CORE_FIT = re.compile(r"\b(superintendent|project (coordinator|manager)|estimator|roof\w*|construction|foreman|"
                       r"field (operations|supervisor|manager|service|sales)|outside sales|sales (rep|representative|consultant)|"
                       r"canvass\w*|safety|site (lead|supervisor|manager)|crew (lead|leader|supervisor)|remodel\w*|"
                       r"restoration|ironworker|installer|inspector|claims adjuster|home improvement|territory|maintenance|"
                       r"facilit\w*|property|handyman|data cent(er|re)|build-?out|general contractor)\b", re.I)


def _skill_matches(app, job) -> list[str]:
    text = f"{job.get('title') or ''} {job.get('description') or ''}"
    return [s for s in app["skills"] if re.search(rf"\b{re.escape(s)}\b", text, re.I)]


def fit_level(app, job) -> str:
    """'direct' when the posting is the kind of work in the resume, else 'transferable'."""
    if _CORE_FIT.search(job.get("title") or ""):
        return "direct"
    return "direct" if len(_skill_matches(app, job)) >= 3 else "transferable"


def _is_ai(job) -> bool:
    return "remote_ai" in (job.get("track") or "") or bool(
        re.search(r"\b(AI|ML|LLM|machine learning|artificial intelligence|data annotation)\b", job.get("title") or ""))


def _background(app, job, d):
    if not app["headline"]:
        return "[[EDIT: one sentence on why your background fits this role]]"
    fit = fit_level(app, job)
    if fit == "direct" and _is_ai(job):  # e.g. AI data-center build-out: the construction side fits
        return _pick([
            f"I'm {app['headline']}, so the build-and-deliver side of this role is familiar ground, and I'm deliberately moving into AI infrastructure work.",
            f"After 30 years running construction projects and crews, I'm steering that experience toward AI infrastructure, and this role sits right at that intersection.",
            f"I'm {app['headline']}, and I'm making a deliberate move into AI; the project and field side of this role is where I've spent my career.",
        ], job["id"], "bg")
    if fit == "direct":
        return _pick([
            f"As {app['headline']}, that's right in my wheelhouse.",
            f"I'm {app['headline']}, so that felt like familiar, exciting territory.",
            f"Coming from my work as {app['headline']}, it felt like a natural fit.",
        ], job["id"], "bg")
    if _is_ai(job):  # honest framing: a career move, not claimed AI experience
        return _pick([
            f"I'm {app['headline']}, and I'm deliberately moving into AI-focused work.",
            f"I'm {app['headline']} making a deliberate move into AI, and this role stood out.",
            f"I come from 30 years of field leadership in construction, and I'm intentionally steering that toward AI work.",
        ], job["id"], "bg")
    return _pick([
        f"I'm {app['headline']}, and a lot of what this role asks for is what my field work has demanded every day: organization, follow-through, and keeping people informed.",
        f"My background is {app['headline'].removeprefix('a ').removeprefix('an ')}, and the planning and people side of that work carries over well here.",
        f"I'm {app['headline']}, and I'd bring the same reliability my crews and customers counted on to a new kind of team.",
    ], job["id"], "bg")


def best_accomplishment(app, job) -> str | None:
    """The true accomplishment whose tags best match the posting (title weighs 3x)."""
    accs = app["accomplishments"]
    if not accs:
        return None
    title = (job.get("title") or "").lower()
    desc = (job.get("description") or "").lower()[:6000]
    def score(a):
        if not a["tags"]:
            return 1  # user-supplied in config: always eligible
        return sum(3 * bool(re.search(rf"\b{re.escape(t)}", title)) + bool(re.search(rf"\b{re.escape(t)}", desc)) for t in a["tags"])
    ranked = sorted(accs, key=lambda a: (-score(a), -a["versions"]))
    top = [a for a in ranked if score(a) == score(ranked[0])][:2]
    return _pick([a["text"] for a in top], job["id"], "acc")


def _value(app, job, d):
    acc = best_accomplishment(app, job)
    if acc:
        lead = _pick(["One example of how I work:", "A quick example from my track record:", "Something from my background that feels relevant:"],
                     job["id"], "accl")
        acc = acc.strip().rstrip(".")
        return f"{lead} I {acc}." if not acc.lower().startswith("i ") else f"{lead} {acc}."
    match = _skill_matches(app, job)
    if match:
        return f"My hands-on experience with {match[0]} lines up closely with what the posting describes."
    focus = d["skills"][0] if d["skills"] else "this kind of work"
    return f"[[EDIT: one brief, true accomplishment or idea related to {focus} for {d['company']}]]"


_BLOCKS_CACHE: dict = {}


def load_blocks(cfg) -> dict:
    p = resolve(cfg["followups"]["templates_file"])
    key = (str(p), p.stat().st_mtime)
    if key not in _BLOCKS_CACHE:
        _BLOCKS_CACHE.clear()
        _BLOCKS_CACHE[key] = yaml.safe_load(p.read_text())
    return _BLOCKS_CACHE[key]


_METRO_CACHE: dict = {}


def _metro(cfg) -> MetroMatcher:
    key = id(cfg["metro"])
    if key not in _METRO_CACHE:
        _METRO_CACHE[key] = MetroMatcher(cfg["metro"])
    return _METRO_CACHE[key]


class _Safe(dict):
    def __missing__(self, k):
        return "{" + k + "}"


def _tidy(s: str) -> str:
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r" +([.,;!?])", r"\1", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def template_sequence(job: dict, lead: dict, cfg: dict) -> list[dict]:
    blocks = load_blocks(cfg)
    app = _applicant(cfg)
    d = details(job, cfg)
    company = d["company"]
    contact = (lead.get("contact_name") or "").strip()
    greeting = (blocks["greetings"]["named"] if contact else blocks["greetings"]["team"]).format(name=contact, company=company)
    base = dict(
        greeting=greeting, name=contact or cfg["followups"]["default_contact_name"], company=company,
        title=job.get("title") or "", role=d["role"], city=d["city"], detail=d["detail"],
        Detail=d["detail"][0].upper() + d["detail"][1:], applicant_name=app["name"], applicant_first=app["first"],
        background=_background(app, job, d), value=_value(app, job, d),
    )
    out, used_signoffs = [], set()
    for touch in (1, 2, 3):
        t = blocks["touches"][touch]
        humor_pool = list(t.get("humor") or [])
        if d["in_metro"] and t.get("humor_atlanta"):
            humor_pool += t["humor_atlanta"]
        signoffs = [s for s in blocks["signoffs"] if s not in used_signoffs] or blocks["signoffs"]
        signoff = _pick(signoffs, job["id"], touch, "sig")
        used_signoffs.add(signoff)
        det = d["detail"] if touch < 3 else d["detail_later"]
        vals = _Safe(dict(base, detail=det, Detail=det[0].upper() + det[1:]), humor=_pick(humor_pool, job["id"], touch, "humor"))
        subject = _pick(t["subjects"], job["id"], touch, "subj").format_map(vals)
        body = _pick(t["bodies"], job["id"], touch, "body").format_map(vals)
        body = _tidy(body) + f"\n\n{signoff}\n{app['signature']}"
        out.append({"touch": touch, "subject": _tidy(subject), "body": body, "generator": "templates",
                    "company": company, "title": job.get("title")})
    return out


# ------------------------------------------------------------------ local LLM

LLM_PROMPT = """You write short, professional follow-up emails for a job applicant.
Write THREE distinctly different plain-text emails (no markdown) about this job.
Rules: each body under 110 words; varied subject lines; light charm and tasteful,
clean workplace-safe humor; no sarcasm, no desperation; never use clichés like
"just circling back", "touching base", "hope this finds you well".
Do not invent facts about the applicant: use only the facts given; where a fact is
missing write a placeholder in double square brackets like [[EDIT: ...]].
If APPLICANT.fit is "transferable", do NOT claim experience in the posting's field;
frame it honestly as transferable strengths or a deliberate career move.
Touch 1: warm, confident intro tying ONE specific detail from the posting to the applicant.
Touch 2: friendly nudge that adds value (idea, skill match, or brief accomplishment) plus one light, clever line.
Touch 3: gracious, witty last check-in that leaves the door open.
Greeting: "{greeting}". End each body with a short sign-off word (e.g. "Best,") and NO name; the signature is appended automatically.

APPLICANT: {applicant_json}
JOB: {title} at {company}, {location}
POSTING DETAILS: {details_json}
POSTING EXCERPT:
{excerpt}

Reply with ONLY JSON: {{"emails":[{{"touch":1,"subject":"...","body":"..."}},{{"touch":2,...}},{{"touch":3,...}}]}}"""


def llm_sequence(job: dict, lead: dict, cfg: dict) -> list[dict] | None:
    from .llm import ollama_available

    if not (cfg["llm"].get("enabled") and cfg["followups"].get("use_llm", True) and ollama_available(cfg)):
        return None
    blocks = load_blocks(cfg)
    app = _applicant(cfg)
    d = details(job, cfg)
    contact = (lead.get("contact_name") or "").strip()
    greeting = (blocks["greetings"]["named"] if contact else blocks["greetings"]["team"]).format(
        name=contact, company=d["company"])
    prompt = LLM_PROMPT.format(
        greeting=greeting,
        applicant_json=json.dumps({"headline": app["headline"], "skills": app["skills"],
                                    "accomplishments": [a["text"] for a in app["accomplishments"]],
                                    "fit": fit_level(app, job)}),
        title=job.get("title"), company=job.get("company"), location=job.get("location"),
        details_json=json.dumps({k: d[k] for k in ("role", "city", "skills", "hook")}),
        excerpt=(job.get("description") or "")[:2500],
    )
    try:
        r = httpx.post(cfg["llm"]["ollama_url"].rstrip("/") + "/api/generate",
                       json={"model": cfg["llm"]["model"], "prompt": prompt, "format": "json", "stream": False},
                       timeout=180)
        r.raise_for_status()
        emails = json.loads(r.json()["response"])["emails"]
        out = []
        for touch in (1, 2, 3):
            e = next(x for x in emails if int(x.get("touch")) == touch)
            out.append({"touch": touch, "subject": e["subject"].strip(), "company": job.get("company"), "title": job.get("title"),
                        "body": e["body"].strip() + "\n" + app["signature"], "generator": "llm"})
        problems = lint(out, cfg)
        if problems:
            log.warning("LLM drafts rejected (%s); using templates", "; ".join(problems))
            return None
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("LLM draft generation failed (%s); using templates", e)
        return None


def lint(seq: list[dict], cfg: dict, max_words: int = 125) -> list[str]:
    banned = [b.lower() for b in load_blocks(cfg).get("banned_phrases") or []]
    problems = []
    subjects = set()
    for e in seq:
        words = len(e["body"].split())
        if words > max_words:
            problems.append(f"touch {e['touch']} has {words} words")
        low = (e["subject"] + " " + e["body"]).lower()
        for own in (e.get("company"), e.get("title")):  # a company literally named "Synergy" is fine
            if own:
                low = low.replace(own.lower(), " ")
        problems += [f"touch {e['touch']} uses banned phrase '{b}'" for b in banned if b in low]
        subjects.add(e["subject"].lower())
    if len(subjects) < len(seq):
        problems.append("subject lines repeat")
    return problems


def generate_sequence(job: dict, lead: dict, cfg: dict) -> list[dict]:
    return llm_sequence(job, lead, cfg) or template_sequence(job, lead, cfg)
