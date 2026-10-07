"""Per-job follow-up copy: posting-detail extraction + rich rotating templates,
with an optional local-LLM (Ollama) writer. Produces DRAFTS only."""
from __future__ import annotations

import hashlib
import json
import logging
import re

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
        # No company name (the sentence usually names it already) and no town: "right here in <city>"
        # would claim the applicant lives/works there.
        options.append("the scope of the role")
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


def ranked_accomplishments(app, job) -> list[str]:
    """All true accomplishments, best match for this posting first (title weighs 3x)."""
    title = (job.get("title") or "").lower()
    desc = (job.get("description") or "").lower()[:6000]

    def score(a):
        if not a["tags"]:
            return 1
        return sum(3 * bool(re.search(rf"\b{re.escape(t)}", title)) + bool(re.search(rf"\b{re.escape(t)}", desc)) for t in a["tags"])
    return [a["text"] for a in sorted(app["accomplishments"], key=lambda a: (-score(a), -a["versions"]))]


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
#
# A small open-weights model served by a LOCAL Ollama (free, no API keys) writes each
# touch from the posting + the applicant's corroborated facts. Every touch is
# validated (length, no placeholders, banned phrases, forbidden/unverified claims,
# numbers must come from the facts or the posting); a touch that fails twice falls
# back to the template version. Greeting, sign-off and signature are added in code,
# so the signature (name/phone/email from config) is always exact.

# Claims that are NOT confirmed and must never appear in a draft, whatever the model says.
FORBIDDEN_CLAIMS = re.compile(
    r"\bPMP\b|project management professional|\bLEED\b|ohio university|\bbachelor'?s?\b|\bB\.?S\.? (degree|in)\b|"
    r"\bmaster'?s degree\b|\b14 (project managers|PMs)\b|supervised 14|\$\s?1[36](\.\d+)?\s?(m\b|mm\b|million)|"
    r"\b1[36](\.\d+)? million\b|\$\s?16,?000,?000|\$\s?13,?000,?000|\bHaag\b|\bveteran\b",
    re.I)
# Claims of AI/ML work experience (only blocked when fit is "transferable": career-changers have none to claim).
_AI_EXPERIENCE_CLAIM = re.compile(
    r"\b(my|years of|extensive|deep|proven|hands-on) (experience|background|expertise|work) (in|with|building|training) "
    r"(AI|ML|LLMs?|machine learning|artificial intelligence|deep learning|data science|model)", re.I)
_PLACEHOLDER = re.compile(r"\[\[|\]\]|\[[A-Z][^\]]{1,40}\]|\{[a-z_ ]+\}|<[a-z_ ]+>|\bXX+\b|lorem ipsum", re.I)
_GREETING_LINE = re.compile(r"^\s*(hi|hello|hey|dear|greetings|good (morning|afternoon))\b[^\n]{0,60}[,:!]?\s*$", re.I)
_SIGNOFF_LINE = re.compile(r"^\s*(best|thanks|thank you|many thanks|warm(est)? regards|regards|kind regards|sincerely|cheers|"
                           r"all the best|with (thanks|gratitude|appreciation)|respectfully|talk soon|looking forward)[^\n]{0,30}[,.!]?\s*$", re.I)
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")

LLM_SYSTEM = """You ghost-write short follow-up emails for a job applicant. Plain text only, no markdown, no emojis.
Voice: professional, warm, confident, specific; light charm and ONE small, clean, workplace-safe touch of humor.
Never desperate, never sarcastic, no exclamation-mark spam.
TRUTH RULES (critical):
- Use ONLY the applicant facts provided. Never invent credentials, degrees, certifications, employers, job titles,
  numbers, metrics, tools or experience. If a fact is not listed, do not claim it.
- Never mention PMP, LEED, a bachelor's degree, Ohio University, supervising 14 project managers, or revenue figures.
- Do not claim to have already spoken with anyone, been referred, or met the reader.
- If FIT is "transferable", do NOT claim experience in the posting's field; present it honestly as transferable
  strengths or a deliberate career move.
- Only state facts about the company that appear in the posting excerpt.
- LOCATION: never say or imply the applicant lives in, is from, is based in, or has worked in the job's city or any
  place not named in his facts. No "right here in <city>", "here in <city>", "local to <city>", "in <city> where I've...".
  Mention the job's city only as where the role is. Follow the LOCATION RULE in the prompt exactly.
- The subject line must be accurate: name the role or a real fact. Never turn one project into years of expertise
  (no "a decade of"), never invent words.
FORMAT: Write in first person as the applicant. The BODY holds ONLY the message paragraphs (2 short paragraphs separated by a blank line, 50-90 words total, at most 6 sentences).
NO greeting line, NO sign-off, NO name or signature: those are added automatically.
Never use: "circling back", "touching base", "just checking in", "hope this finds you well", "per my last email",
"synergy", "rockstar", "ninja", placeholders in brackets."""

LLM_TOUCH = {
    1: ("THIS EMAIL = touch 1 of 3, sent the day the applicant applied. A warm, confident introduction: say he just applied "
        "for the {role} role. Connect this posting detail: {detail} -> to this applicant fact: \"{fact_a}\". "
        "Add a touch of charm (one playful half-sentence, e.g. about reading the posting twice). "
        "End with an easy, low-pressure invitation to talk."),
    2: ("THIS EMAIL = touch 2 of 3, sent a few days later. A friendly nudge that ADDS VALUE: lead with this specific "
        "accomplishment: \"{fact_b}\" and say in one sentence how that would help {company} in this role. "
        "Include ONE light, clever line of humor, in the spirit of these examples (adapt one or write your own): {humor}"),
    3: ("THIS EMAIL = touch 3 of 3, the final note, about a week later. Gracious and witty, short (50-80 words). Say this is the "
        "last note, no guilt-tripping, still interested in {role}, happy to be considered for future roles at {company}. "
        "You have never spoken with them, so do not mention any conversation. "
        "End with one light line in the spirit of these examples (adapt one or write your own): {humor}"),
}

LLM_PROMPT = """APPLICANT: {name}
HEADLINE: {headline}
FIT for this job: {fit}{ai_note}
APPLICANT FACTS (the only things you may claim):
{facts}

JOB: {title}
COMPANY: {company}
LOCATION: {location}
LOCATION RULE: {location_rule}
POSTING EXCERPT:
{excerpt}

{touch_instruction}
{avoid}
Reply in exactly this format and nothing else:
SUBJECT: <subject line, 3-9 words, specific to this job>
BODY:
<the paragraphs only>"""


def applicant_facts(cfg) -> dict:
    """Corroborated facts only: headline, skills, `use` accomplishments without a confirm flag,
    and certifications/software that appear in most resume versions. Never Ohio University."""
    app = _applicant(cfg)
    certs, software = [], []
    pf = (cfg.get("applicant") or {}).get("profile_file")
    p = resolve(pf) if pf else None
    if p and p.exists():
        data = json.loads(p.read_text())

        def solid(it):
            return (not it.get("confirm") and it.get("versions", 0) >= 10
                    and not FORBIDDEN_CLAIMS.search(it.get("item", "")))
        certs = [c["item"] for c in data.get("certifications") or [] if solid(c) and "compan" not in c["item"].lower()]
        certs += [e["item"] for e in data.get("education") or [] if solid(e)]
        software = [re.sub(r"\s*\(.*?\)", "", s["item"]) for s in data.get("software") or [] if solid(s)]
    accs = [a["text"] for a in app["accomplishments"] if not FORBIDDEN_CLAIMS.search(a["text"])]
    a = cfg.get("applicant") or {}
    return {"name": app["name"], "first": app["first"], "headline": app["headline"], "skills": app["skills"],
            "accomplishments": accs, "certifications": certs, "software": software,
            "location_fact": (a.get("location_fact") or "").strip().rstrip("."),
            "worked_counties": [c.strip() for c in a.get("worked_counties") or [] if c and c.strip()],
            "home_base": (a.get("home_base") or "").strip(), "work_area": (a.get("work_area") or "").strip()}


def facts_text(facts: dict, job: dict | None = None, app=None) -> str:
    lines = []
    if facts["headline"]:
        lines.append(f"- Is {facts['headline']}.")
    accs = list(facts["accomplishments"])
    if job is not None and app is not None:  # most relevant accomplishment first
        best = best_accomplishment(app, job)
        if best:
            accs.sort(key=lambda a: a.rstrip(".") != best.rstrip("."))
    lines += [f"- {a[0].upper() + a[1:]}." for a in accs]
    if facts["certifications"]:
        lines.append("- Credentials: " + "; ".join(facts["certifications"]) + ".")
    if facts["software"]:
        lines.append("- Software used: " + ", ".join(facts["software"]) + ".")
    if facts["skills"]:
        lines.append("- Skills: " + ", ".join(facts["skills"]) + ".")
    if facts.get("location_fact"):
        lines.append(f"- Location: {facts['location_fact']}. Never claim ties to a specific town.")
    return "\n".join(lines)


def greeting_for(job: dict, lead: dict, cfg: dict) -> str:
    blocks = load_blocks(cfg)
    contact = (lead.get("contact_name") or "").strip()
    company = display_company(job.get("company"))
    return (blocks["greetings"]["named"] if contact else blocks["greetings"]["team"]).format(name=contact, company=company)


def _clean_body(text: str) -> str:
    """Drop any greeting / sign-off / name lines the model added despite instructions."""
    text = text.replace("\\n", "\n").replace("\r", "")
    text = re.sub(r"[*_#`]+", "", text)
    lines = [ln.rstrip() for ln in text.strip().split("\n")]
    while lines and (not lines[0].strip() or _GREETING_LINE.match(lines[0])):
        lines.pop(0)
    while lines and (not lines[-1].strip() or _SIGNOFF_LINE.match(lines[-1])
                     or (len(lines[-1].split()) <= 4 and not lines[-1].rstrip().endswith((".", "?", "!", ")")))):
        lines.pop()  # trailing sign-off / bare name / phone
    text = "\n".join(lines)
    text = re.sub(r"\s*\b(best|kind|warm|warmest)( regards)?[,.!]?\s*$", "", text, flags=re.I) if re.search(
        r"[.!?]\s*(best|kind|warm|warmest)( regards)?[,.!]?\s*$", text, re.I) else text
    return _tidy(text)


_FLUFF = re.compile(r"\b(excited|excite|believe|confident|eager|passion\w*|great fit|asset|align\w*|genuinely|truly|"
                    r"unique|valuable|thrilled|opportunity to|look forward|wanted to (express|reach|say)|"
                    r"I'm (sure|certain)|strong fit|perfect fit|natural fit|I am writing)\b", re.I)


def _key_tokens(text: str) -> set[str]:
    stop = {"years", "running", "across", "while", "after", "early", "career", "worked", "helped", "people", "their", "there",
            "which", "about", "would", "could", "through", "during", "including"}
    return {w for w in re.findall(r"[a-z0-9][a-z0-9,'-]{3,}", (text or "").lower()) if w not in stop}


def _uses_fact(text: str, fact: str, need: int = 2) -> bool:
    return len(_key_tokens(fact) & _key_tokens(text)) >= need


def trim_body(body: str, max_words: int, protect: list[str] | None = None) -> str:
    """Small models run long. Drop filler sentences (never the first sentence or the closing
    paragraph) until the body fits; facts and the call to action survive."""
    paras = [re.split(r"(?<=[.!?])\s+", p.strip()) for p in body.split("\n\n") if p.strip()]
    count = lambda: sum(len(x.split()) for p in paras for x in p)
    while count() > max_words:
        cands = [(pi, si) for pi, p in enumerate(paras[:-1] if len(paras) > 1 else paras)
                 for si, x in enumerate(p) if not (pi == 0 and si == 0) and len(p) > 1]
        if not cands:
            break
        def keep_score(c):
            x = paras[c[0]][c[1]]
            factual = any(_uses_fact(x, f) for f in protect or []) or bool(_NUM.search(x))
            return (-factual, len(_FLUFF.findall(x)), len(x.split()))
        pi, si = max(cands, key=keep_score)
        del paras[pi][si]
    return "\n\n".join(" ".join(p) for p in paras if p)


def _parse_draft(raw: str) -> tuple[str, str]:
    """'SUBJECT: ...\nBODY:\n...' (plain text is far more reliable than JSON for a 3B model).
    Tolerates a missing SUBJECT/BODY label (first short line = subject)."""
    raw = re.sub(r"[*_#`]+", "", raw or "").strip()
    raw = re.sub(r"^\s*\(?(blank line|empty line|line break)\)?\s*$", "", raw, flags=re.I | re.M)
    m = re.search(r"subject(?: line)?\s*:\s*(.+)", raw, re.I)
    if m:
        subject = m.group(1)
        rest = raw[m.end():]
    else:
        first, _, rest = raw.partition("\n")
        subject = first if len(first.split()) <= 12 and not first.rstrip().endswith((".", "?", "!")) else ""
        rest = rest if subject else raw
    b = re.search(r"\bbody\s*:\s*(.*)", rest, re.I | re.S)
    body = b.group(1) if b else rest
    subject = re.sub(r"^(re|fwd?)\s*:\s*", "", " ".join(subject.split()), flags=re.I).strip(" \"'<>")
    if subject.isupper():
        subject = subject.title()
    return subject, _clean_body(body)


# ------------------------------------------------------------------ location claims
#
# A draft may only claim local ties ("right here in X", "where I've worked", "local to X") to the
# places listed in applicant.home_area_places (config.local.yaml). Everything else, above all the
# job's own city, is mentioned only as where the role is.

_US_STATES = ("Alabama Alaska Arizona Arkansas California Colorado Connecticut Delaware Florida Georgia Hawaii Idaho "
              "Illinois Indiana Iowa Kansas Kentucky Louisiana Maine Maryland Massachusetts Michigan Minnesota "
              "Mississippi Missouri Montana Nebraska Nevada Ohio Oklahoma Oregon Pennsylvania Tennessee Texas Utah "
              "Vermont Virginia Washington Wisconsin Wyoming").split() + [
    "New Hampshire", "New Jersey", "New Mexico", "New York", "North Carolina", "North Dakota", "Rhode Island",
    "South Carolina", "South Dakota", "West Virginia"]
_OTHER_PLACES = ["Sarasota", "Tampa", "Tampa Bay", "Orlando", "Miami", "Jacksonville", "Savannah", "Augusta", "Macon",
                 "Columbus", "Athens", "Jasper", "Rome", "Dalton", "Chattanooga", "Birmingham", "Charlotte", "Nashville",
                 "Dallas", "Houston", "Chicago", "Royston", "Calhoun", "Valdosta", "Albany", "Warner Robins",
                 "DeKalb", "Gwinnett", "Rockdale", "Coweta", "Pickens", "Forsyth", "Paulding", "Bartow", "Cherokee"]
_PLACE_WORD = r"[A-Z][A-Za-z'-]*"
_PLACE = rf"{_PLACE_WORD}(?:[ -]{_PLACE_WORD}){{0,3}}"
# "right here in X", "here in the X area", "here in beautiful X"
_HERE_IN = re.compile(rf"\b[Hh]ere\s+in\s+(?:the\s+)?(?:(?:beautiful|sunny|historic|greater|metro|downtown)\s+)?(?P<p>{_PLACE})")
# self-locating phrases: only flagged when the place is a known place that is not allowed
_SELF_LOC = re.compile(
    rf"\b(?:local\s+to|locally\s+in|based\s+(?:here\s+)?(?:in|out\s+of)|li(?:ve|ved|ving)\s+in|reside[sd]?\s+in|grew\s+up\s+in|"
    rf"(?:I'm|I\s+am|I've\s+been)\s+(?:from|in)|hometown\s+(?:of\s+)?|home\s+(?:base\s+)?in|neighbou?rs?\s+in|roots\s+in|"
    rf"(?:I've|I\s+have|I)\s+(?:lived|worked|roofed|built|completed|served|spent\s+[\w\s]{{0,20}}?)\s+(?:\w+\s+){{0,6}}?in|"
    rf"(?:all\s+)?(?:across|throughout|around)(?:\s+the)?)\s+(?:the\s+)?(?P<p>{_PLACE})")
# a sentence naming a non-allowed place + any of these = a claim of local ties / past work there
_LOCAL_MARKER = re.compile(
    r"\b(here|local\w*|neighbou?r\w*|hometown|home\s*base|my\s+home|community|where\s+I|based|li(?:ve|ved|ves|ving)|"
    r"resid\w+|grew\s+up|familiar\s+(?:ground|territory|streets|turf|area)|backyard|my\s+(?:own\s+)?(?:town|city|area|county)|"
    r"(?:I've|I\s+have|I)\s+(?:lived|worked|roofed|built|completed|served|spent|been\s+(?:working|building|roofing))|"
    r"roofs|projects\s+a\s+month|years\s+(?:in|around|across))\b", re.I)


# Metro-Atlanta town -> county (public geography; private additions via applicant.town_counties).
TOWN_COUNTY = {
    "Atlanta": "Fulton", "Alpharetta": "Fulton", "Sandy Springs": "Fulton", "Johns Creek": "Fulton", "Roswell": "Fulton",
    "Milton": "Fulton", "College Park": "Fulton", "East Point": "Fulton", "Hapeville": "Fulton", "Union City": "Fulton",
    "Fairburn": "Fulton", "Palmetto": "Fulton", "South Fulton": "Fulton", "Chattahoochee Hills": "Fulton",
    "Marietta": "Cobb", "Kennesaw": "Cobb", "Smyrna": "Cobb", "Acworth": "Cobb", "Austell": "Cobb", "Mableton": "Cobb",
    "Vinings": "Cobb", "Hiram": "Paulding", "Dallas": "Paulding", "Cartersville": "Bartow", "Adairsville": "Bartow",
    "Emerson": "Bartow", "Euharlee": "Bartow", "Kingston": "Bartow", "Woodstock": "Cherokee", "Canton": "Cherokee",
    "Holly Springs": "Cherokee", "Ball Ground": "Cherokee", "Waleska": "Cherokee", "Decatur": "DeKalb",
    "Dunwoody": "DeKalb", "Brookhaven": "DeKalb", "Chamblee": "DeKalb", "Doraville": "DeKalb", "Tucker": "DeKalb",
    "Stone Mountain": "DeKalb", "Lithonia": "DeKalb", "Stonecrest": "DeKalb", "Lawrenceville": "Gwinnett",
    "Peachtree Corners": "Gwinnett", "Norcross": "Gwinnett", "Buford": "Gwinnett", "Suwanee": "Gwinnett",
    "Snellville": "Gwinnett", "Lilburn": "Gwinnett", "Duluth": "Gwinnett", "Sugar Hill": "Gwinnett",
    "Jonesboro": "Clayton", "Forest Park": "Clayton", "Morrow": "Clayton", "Riverdale": "Clayton", "Lake City": "Clayton",
    "McDonough": "Henry", "Stockbridge": "Henry", "Locust Grove": "Henry", "Hampton": "Henry",
    "Peachtree City": "Fayette", "Fayetteville": "Fayette", "Tyrone": "Fayette", "Douglasville": "Douglas",
    "Conyers": "Rockdale", "Newnan": "Coweta", "Loganville": "Walton", "Monroe": "Walton", "Cumming": "Forsyth",
    "Flowery Branch": "Hall", "Gainesville": "Hall", "Oakwood": "Hall", "Braselton": "Jackson", "Dawsonville": "Dawson",
    "Villa Rica": "Carroll", "Carrollton": "Carroll", "Covington": "Newton", "Griffin": "Spalding", "Winder": "Barrow",
    "Jasper": "Pickens", "Royston": "Franklin", "Rockmart": "Polk",
}
_COUNTY_NAMES = sorted(set(TOWN_COUNTY.values()) | {"Paulding", "Bartow", "Cherokee", "Cobb", "Fulton"})


def county_of(city: str, cfg) -> str:
    m = {k.lower(): v for k, v in TOWN_COUNTY.items()}
    m.update({k.lower(): v for k, v in ((cfg.get("applicant") or {}).get("town_counties") or {}).items()})
    return m.get((city or "").strip().lower(), "")


def worked_counties(cfg) -> list[str]:
    return [c.strip() for c in (cfg.get("applicant") or {}).get("worked_counties") or [] if c and c.strip()]


def _county_ok(place: str, cfg) -> bool:
    key = re.sub(r"\s+count(y|ies)$", "", place.strip(), flags=re.I).lower()
    return key in {c.lower() for c in worked_counties(cfg)}


def _home_places(cfg) -> list[str]:
    a = cfg.get("applicant") or {}
    return [p.strip() for p in a.get("home_area_places") or [] if p and p.strip()]


def _place_key(p: str) -> str:
    p = re.sub(r"'s$", "", p.strip().lower())
    p = re.sub(r"^(the|greater|metro|metropolitan|downtown)\s+", "", p)
    p = re.sub(r"\s+(area|metro|region|county|community|market)$", "", p)
    return p


def place_allowed(place: str, cfg) -> bool:
    """True when `place` is in the applicant's real home/work area (applicant.home_area_places)."""
    key = _place_key(place)
    for h in _home_places(cfg):
        hk = _place_key(h)
        if key == hk or key.startswith(hk + " ") or key.startswith(hk + ","):
            return True
    return False


def job_city(job: dict) -> str:
    loc = (job.get("location") or "").split(",")[0].strip()
    return "" if not loc or re.search(r"remote|anywhere|united states|^usa?$", loc, re.I) else loc


def _known_places(cfg, job) -> list[str]:
    m = cfg.get("metro") or {}
    places = list(m.get("towns") or []) + list(m.get("towns_require_state") or []) + _US_STATES + _OTHER_PLACES
    places = [re.sub(r",.*", "", p).strip() for p in places]
    city = job_city(job)
    if city:
        places.append(city)
    return sorted({p for p in places if p}, key=len, reverse=True)


def location_problems(subject: str, body: str, job: dict, cfg: dict, facts: dict | None = None) -> list[str]:
    """Claims of living/working somewhere the facts don't support (empty list = OK)."""
    text = f"{subject}.\n{body}"
    # the company / job title / fact entities may contain town names ("Royston Plant", "Cora Texas Sugar Mill")
    strip = [job.get("company") or "", display_company(job.get("company")), job.get("title") or ""]
    if facts is not None:
        ft = facts_text(facts)
        for ent in re.findall(r"[A-Z][\w'-]*(?: [A-Z][\w'-]*)+", ft):
            w = ent.split()
            strip += [" ".join(w[:k]) for k in range(2, len(w) + 1)]
    for s_ in sorted({x for x in strip if len(x) > 3}, key=len, reverse=True):
        text = re.sub(re.escape(s_), " ", text, flags=re.I)
    probs = []
    known = _known_places(cfg, job)
    known_re = re.compile(r"\b(" + "|".join(re.escape(p) for p in known) + r")\b") if known else None
    for m in _HERE_IN.finditer(text):
        p = m.group("p")
        # "here in <town>" is never OK (not even a home-area town); only regions like "here in metro Atlanta"
        region = re.search(r"\bmetro\b|\bcounty\b|\bGeorgia\b", m.group(0), re.I)
        if not (region and place_allowed(p, cfg)):
            probs.append(f"location claim '{m.group(0).strip()}'")
    for m in _SELF_LOC.finditer(text):
        p = m.group("p")
        residence = re.search(r"local|based|li(?:ve|ved|ving)|resid|grew|home|neighbo|roots|from|I'm|I am|I've been", m.group(0), re.I)
        if known_re and known_re.match(p) and not place_allowed(p, cfg) and (residence or not _county_ok(p, cfg)):
            probs.append(f"location claim '{' '.join(m.group(0).split())}'")
    # residence claims about a county other than the home base ("based in Fulton County")
    for m in re.finditer(r"\b(?:based|li(?:ve|ved|ving)|resid\w*|grew\s+up|local|home(?:\s+base)?|from)\s+(?:here\s+)?(?:in|to|of|out\s+of)?\s*"
                         r"(?:the\s+)?(?P<p>[A-Z][A-Za-z]+\s+County)\b", text):
        if not place_allowed(m.group("p"), cfg):
            probs.append(f"location claim '{' '.join(m.group(0).split())}'")
    # counties: only the ones he has actually worked in
    for m in re.finditer(r"\b((?:[A-Z][A-Za-z]+(?:,\s*|\s+and\s+|\s*&\s*|\s+or\s+)?)+)\s+[Cc]ount(?:y|ies)\b", text):
        for name in re.findall(r"[A-Z][A-Za-z]+", m.group(1)):
            if name in ("And", "Or", "The") or name.lower() in {c.lower() for c in worked_counties(cfg)}:
                continue
            if name in _COUNTY_NAMES or m.group(1).strip() == name:
                probs.append(f"county claim '{name} County' (not a county he has worked in)")
    if known_re:
        for sent in re.split(r"(?<=[.!?])\s+|\n+", text):
            for m in known_re.finditer(sent):
                p = m.group(1)
                if place_allowed(p, cfg) or (_county_ok(p, cfg) and not re.search(
                        r"\bhere\b|local|neighbo|hometown|home\s*base|li(?:ve|ved|ves|ving)\b|resid|grew up", sent, re.I)):
                    continue
                mk = _LOCAL_MARKER.search(sent)
                if mk:
                    probs.append(f"location claim: '{p}' with '{mk.group(0)}'")
                    break
    city = job_city(job)
    if city and not place_allowed(city, cfg) and re.search(
            rf"\b(?:in|of|around|near)\s+{re.escape(city)}\s*,?\s*where\b", text, re.I):
        probs.append(f"location claim 'in {city} where ...'")
    return list(dict.fromkeys(probs))


# Words a 3B model has mangled before, invented "-ship" nouns, and leaked prompt/meta text.
_MANGLED = re.compile(r"\b(expertship|decature|leaded|experiance|recieve[ds]?|managment|oppertunit\w*|sucess\w*)\b", re.I)
_SHIP_OK = {"leadership", "partnership", "ownership", "relationship", "relationships", "internship", "membership",
            "apprenticeship", "sponsorship", "championship", "scholarship", "worship", "hardship", "craftsmanship",
            "citizenship", "stewardship", "friendship", "mentorship", "fellowship", "salesmanship", "workmanship",
            "dealership", "readership", "authorship", "township", "kinship", "entrepreneurship", "showmanship",
            "sportsmanship", "partnerships", "hardships", "dealerships", "championships", "internships"}
_META = re.compile(r"(^|\n)\s*(note|p\.?\s?s|blank line|empty line|subject|body)\s*[:.)]|\(blank line\)|\bthis (reply|email|note) is sent\b|"
                   r"\btouch [123]\b|\badding value by\b|\bas instructed\b", re.I)
_TIME_SPAN = re.compile(r"\b(?:a|one|over\s+a|nearly\s+a|almost\s+a|full|single|past)\s+decade\b|\bdecade\s+of\b", re.I)


def _near_miss_places(text: str, cfg, job) -> list[str]:
    """Capitalized words one typo away from a known place/company word ('Decature' for Decatur)."""
    import difflib
    vocab = {w.lower() for p in _known_places(cfg, job) for w in p.split()}
    vocab |= {w.lower() for w in re.findall(r"[A-Za-z]{4,}", f"{job.get('company') or ''} {job.get('title') or ''}")}
    bad = []
    for w in set(re.findall(r"\b[A-Z][a-z]{4,}\b", text)):
        lw = w.lower()
        if lw in vocab or lw.rstrip("s") in vocab:
            continue
        close = difflib.get_close_matches(lw, vocab, n=1, cutoff=0.88)
        if close and lw not in (close[0] + "s", close[0] + "n", close[0] + "ns", close[0] + "es"):
            bad.append(f"{w}?{close[0]}")
    return bad


def _norm_num(n: str) -> str:
    return n.replace(",", "").rstrip(".")


def validate_draft(subject: str, body: str, job: dict, cfg: dict, facts: dict, fit: str,
                   min_words: int = 35, max_words: int = 125) -> list[str]:
    """Problems with one model-written touch (empty list = OK). `body` = paragraphs only."""
    probs = []
    words = len(body.split())
    if not min_words <= words <= max_words:
        probs.append(f"{words} words")
    if not 3 <= len(subject) <= 80 or "\n" in subject:
        probs.append("bad subject length")
    both = f"{subject}\n{body}"
    if _PLACEHOLDER.search(both):
        probs.append("placeholder left in")
    m = FORBIDDEN_CLAIMS.search(both)
    if m:
        probs.append(f"unverified claim '{m.group(0)}'")
    if fit == "transferable" and _AI_EXPERIENCE_CLAIM.search(both):
        probs.append("claims AI/ML experience")
    if re.search(r"\b(attached|attachment|enclosed|I've included my resume)\b", both, re.I):
        probs.append("mentions an attachment")
    if re.search(r"\b(as (we|I) discussed|as mentioned|our (previous |earlier |last |recent )?(call|conversations?|chat|discussions?|meeting)|"
                 r"when we (spoke|met|talked)|we (spoke|talked|met|discussed)|referred me|my last (email|note)|following our)\b", both, re.I):
        probs.append("invents prior contact")
    low = both.lower()
    for own in (job.get("company"), job.get("title")):
        if own:
            low = low.replace(own.lower(), " ")
    banned = [b.lower() for b in load_blocks(cfg).get("banned_phrases") or []]
    probs += [f"banned phrase '{b}'" for b in banned if b in low]
    # every number must come from the facts or the posting (no invented metrics)
    source = " ".join([facts_text(facts), job.get("title") or "", job.get("company") or "", job.get("location") or "",
                       (job.get("description") or "")[:6000]])
    allowed = {_norm_num(n) for n in _NUM.findall(source)}
    bad = [n for n in _NUM.findall(both) if _norm_num(n) not in allowed]
    if bad:
        probs.append(f"ungrounded number(s) {bad[:3]}")
    probs += location_problems(subject, body, job, cfg, facts)
    m = _MANGLED.search(both)
    if m:
        probs.append(f"typo '{m.group(0)}'")
    odd = [w for w in re.findall(r"\b[A-Za-z]+ship\b", both) if w.lower() not in _SHIP_OK]
    if odd:
        probs.append(f"invented word '{odd[0]}'")
    near = _near_miss_places(both, cfg, job)
    if near:
        probs.append(f"misspelled name {near[:2]}")
    if _META.search(body):
        probs.append("prompt/meta text leaked into body")
    first = (facts.get("first") or "").strip()
    if first and not first.startswith("[[") and re.search(rf"\b{re.escape(first)}\b", body):
        probs.append("refers to the applicant by name (third person)")
    m = _TIME_SPAN.search(both)
    if m:
        probs.append(f"unsupported time span '{m.group(0)}'")
    if re.search(r"\b(AI|ML|machine learning|cloud)\b[\w\s-]{0,12}\b(expert|specialist|veteran|professional|engineer|leader)s?\b",
                 both, re.I):
        probs.append("claims AI/cloud expertise")
    if re.search(r"\bwe (haven't|have not|never) (yet )?(discussed|spoken|talked|met|had a chance)|\bour paths\b|"
                 r"\bour shared\b|\bas you may know\b", both, re.I):
        probs.append("implies a relationship with the reader")
    paras = [x for x in body.split("\n\n") if x.strip()]
    if len(paras) > 3:
        probs.append(f"{len(paras)} paragraphs")
    if re.match(r"\s*[A-Z][a-z]+ed\b(?! by)", body) and not re.match(r"\s*(Excited|Interested|Inspired|Delighted|Pleased|Based)\b", body):
        probs.append("body opens with a sentence fragment")
    co = display_company(job.get("company"))
    # "<Company>'s focus on X" / "the company's commitment to X": X must come from the posting
    post = _key_tokens((job.get("description") or "")[:8000])
    for m in re.finditer(rf"(?:{re.escape(co)}|the company|your company|your team|your organization)(?:'s|\u2019s) "
                         r"(?:focus|commitment|dedication|reputation|emphasis|approach|mission|track record) (?:on|to|for|of)? ?([^,.;]{5,80})",
                         both, re.I):
        toks = _key_tokens(m.group(1))
        if toks and len(toks & post) * 2 < len(toks):
            probs.append(f"ungrounded company claim '{' '.join(m.group(0).split()[:10])}'")
    if co and co != "your team" and any(len(re.findall(re.escape(co), x, re.I)) > 1
                                        for x in re.split(r"(?<=[.!?])\s+|\n+", body)):
        probs.append("company name repeated in one sentence")
    return probs


def location_rule(job: dict, cfg: dict, facts: dict) -> str:
    home = facts.get("home_base") or "(not given)"
    area = facts.get("work_area") or "(not given)"
    city = job_city(job)
    rule = f"The applicant is based in {home}; his work was across {area}."
    if not city:
        return rule + " This is a remote role: do not mention any town."
    county = county_of(city, cfg)
    never = (f" Never write \"here in {city}\" or \"right here in {city}\", and never say he lives in or is local to {city}.")
    if place_allowed(city, cfg):
        return rule + never + " You may say he is based in his home county or has worked across metro Atlanta."
    if county and _county_ok(county, cfg):
        return rule + never + (f" {city} is in {county} County, one of the counties he has roofed in, so you may say he has "
                               f"worked in {county} County or across metro Atlanta, but not that he worked in {city} itself.")
    where = f"{city} ({county} County)" if county else city
    return rule + never + (f" {where} is NOT an area he has worked in: do not claim he worked in {city}"
                           + (f" or {county} County" if county else "") + "; say only \"across metro Atlanta\" if location comes up.")


def llm_sequence(job: dict, lead: dict, cfg: dict) -> list[dict] | None:
    """Three personalized drafts from the local model, or None if Ollama is off/unreachable.
    Touches that fail validation twice are replaced by the template version of that touch."""
    from . import llm

    if not (cfg["llm"].get("enabled") and cfg["followups"].get("use_llm", True) and llm.ollama_available(cfg, model=True)):
        return None
    blocks = load_blocks(cfg)
    app = _applicant(cfg)
    facts = applicant_facts(cfg)
    fit = fit_level(app, job)
    d = details(job, cfg)
    greeting = greeting_for(job, lead, cfg)
    ai_note = (" (an AI-focused role: the applicant is deliberately moving into AI and has NO AI work experience to claim)"
               if _is_ai(job) else "")
    base = dict(name=facts["name"], headline=facts["headline"] or "(not given)", fit=fit, ai_note=ai_note,
                facts=facts_text(facts, job, app), title=job.get("title") or "", company=d["company"],
                location=job.get("location") or "", location_rule=location_rule(job, cfg, facts),
                excerpt=_excerpt(job.get("description") or "", 1800))
    ranked = ranked_accomplishments(app, job) or [facts["headline"] or "steady, reliable follow-through"]
    fact_a = ranked[0]
    fact_b = next((a for a in ranked[1:] if a != fact_a), fact_a)
    humor = {}
    for t in (2, 3):
        pool = list(blocks["touches"][t].get("humor") or [])
        if d["in_metro"]:
            pool = list(blocks["touches"][t].get("humor_atlanta") or []) + pool
        i = _seed(job["id"], t, "llmhumor") % max(len(pool), 1)
        humor[t] = " | ".join(f'"{h}"' for h in (pool[i:] + pool[:i])[:3])
    assign = dict(role=d["role"], company=d["company"], detail=d["detail"], fact_a=fact_a, fact_b=fact_b)
    templates = None
    out, used_signoffs, prev = [], set(), []
    model = cfg["llm"]["model"]
    for touch in (1, 2, 3):
        avoid = ""
        if prev:
            avoid = "Earlier emails in this series (do NOT reuse their subject, opening or main fact):\n" + "\n".join(
                f"- touch {p['touch']} subject: {p['subject']} | opening: {p['opening']}" for p in prev)
        instr = LLM_TOUCH[touch].format(**assign, humor=humor.get(touch, ""))
        prompt = LLM_PROMPT.format(**base, touch_instruction=instr, avoid=avoid)
        draft, problems = None, []
        for attempt in range(int(cfg["llm"].get("draft_attempts") or 3)):
            try:
                raw, stats = llm.generate(cfg, prompt, system=LLM_SYSTEM, temperature=0.75 + 0.1 * attempt,
                                          num_predict=320, seed=_seed(job["id"], touch, attempt) % 100000)
                subject, body = _parse_draft(raw)
                body = trim_body(body, 115, protect=[fact_a, fact_b])
            except Exception as e:  # noqa: BLE001
                problems = [f"generation error: {e}"]
                continue
            problems = validate_draft(subject, body, job, cfg, facts, fit)
            if not body:
                log.debug("unparseable model output: %r", raw[:300])
            if subject.lower() in {p["subject"].lower() for p in prev}:
                problems.append("subject repeats an earlier touch")
            if touch == 1 and not re.search(r"\bappl(y|ied|ying|ication)\b", body, re.I):
                problems.append("touch 1 doesn't say he applied")
            if touch == 2 and not _uses_fact(body, fact_b) and not _uses_fact(body, fact_a):
                problems.append("touch 2 lacks a concrete accomplishment")
            if not problems:
                draft = (subject, body)
                log.info("ollama draft %s touch %d ok in %ss (%s tokens)", job["id"][:8], touch, stats["seconds"], stats["output_tokens"])
                break
            log.info("ollama draft %s touch %d attempt %d rejected: %s", job["id"][:8], touch, attempt + 1, "; ".join(problems))
        if draft is not None and touch == 3 and not re.search(
                r"\b(last|final|finally|wrap\w*|step(ping)? back|closing|trilogy)\b", f"{draft[0]} {draft[1]}", re.I):
            # 3B models often forget "this is the last note"; prepend a clear closer.
            draft = (draft[0] if re.search(r"\b(last|final|closing)\b", draft[0], re.I) else f"Last note on {d['role']}",
                     f"This is my last note about the {d['role']} role. " + draft[1])
        if draft is None:
            log.warning("touch %d for %s: using template (%s)", touch, job["id"][:8], "; ".join(problems))
            templates = templates or {e["touch"]: e for e in template_sequence(job, lead, cfg)}
            e = dict(templates[touch])
            e["generator"] = "templates (llm fallback)"
            out.append(e)
            prev.append({"touch": touch, "subject": e["subject"], "opening": ""})
            continue
        subject, body = draft
        signoffs = [s for s in blocks["signoffs"] if s not in used_signoffs] or blocks["signoffs"]
        signoff = _pick(signoffs, job["id"], touch, "sig")
        used_signoffs.add(signoff)
        full = f"{greeting}\n\n{body}\n\n{signoff}\n{app['signature']}"
        out.append({"touch": touch, "subject": subject, "body": full, "generator": f"ollama:{model}",
                    "company": d["company"], "title": job.get("title")})
        prev.append({"touch": touch, "subject": subject, "opening": " ".join(body.split()[:12])})
    if not out[-1]["body"].rstrip().endswith(app["signature"].splitlines()[-1]):
        return None  # signature missing: should be impossible, but never ship that
    return out


def _excerpt(desc: str, n: int) -> str:
    """Posting text for the prompt: collapse whitespace, drop EEO/benefits boilerplate tails."""
    text = re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", desc or "")).strip()
    cut = re.search(r"(equal opportunity employer|EEO statement|we are an equal|reasonable accommodation)", text, re.I)
    if cut and cut.start() > 400:
        text = text[:cut.start()]
    return text[:n]


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
