"""Company-info enrichment for qualified leads and top matches.

Free sources only, cheapest first:
  1. JobSpy's employer fields stored on the job row (company website, HQ, industry, size,
     description, direct apply link, emails JobSpy read from the posting).
  2. The posting text itself (emails, a named recruiter, ATS links). LinkedIn/ZipRecruiter/
     Glassdoor rows fetched without a description get their posting page on demand (one request).
  3. The posting's ATS: Greenhouse/Lever/Ashby public board APIs (find the same job on the
     company's own board -> direct apply link).
  4. One DuckDuckGo search (ddgs package) to find the company's own domain (and LinkedIn page)
     when nothing above supplied it.
  5. A few public pages on that domain (home, careers, jobs, contact, about): mailto:/plain-text
     emails on the company's own domain, careers/ATS links, phone, one-line description.

Rules: no API keys, no paid services, never guess email patterns, never SMTP-probe, never sign
in, never submit forms; robots.txt is honored, requests are slow and capped, results cached.
Emails are classified HIRING / GENERAL / IGNORE. Only a HIRING address read verbatim from a
cited page (or the posting) may be filled in as a lead's contact, and only when the lead has no
contact yet. Every failure is caught: enrichment can never break `auto`.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from urllib import robotparser
from urllib.parse import parse_qs, urljoin, urlparse

from .normalize import norm_company

log = logging.getLogger("aggregator.enrich")

# ----------------------------------------------------------------------------- emails
EMAIL_RE = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9][A-Za-z0-9._%+-]{0,63}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}(?![A-Za-z0-9-])")
_ASSET_TLD = {"png", "jpg", "jpeg", "gif", "svg", "webp", "css", "js", "ico", "bmp", "tif", "tiff", "pdf", "mp4", "woff", "woff2"}
_JUNK_DOMAINS = re.compile(r"(^|\.)(example\.(com|org|net)|domain\.com|email\.com|yourdomain\.com|yourcompany\.com|company\.com|"
                           r"sentry\.io|sentry-next\.wixpress\.com|wixpress\.com|wix\.com|squarespace\.com|godaddy\.com|"
                           r"mysite\.com|test\.com|address\.com|latofonts\.com|2x\.png)$", re.I)
_BOARD_DOMAINS = re.compile(r"(^|\.)(indeed\.com|glassdoor\.com|linkedin\.com|ziprecruiter\.com|monster\.com|careerbuilder\.com|"
                            r"simplyhired\.com|dice\.com)$", re.I)

HIRING_LOCAL = re.compile(
    r"^(hr|h\.r|humanresources|human[._-]?resources|careers?|jobs?|employment|recruit\w*|talent\w*|hiring|hire\w*|"
    r"resumes?|cv|apply\w*|applications?|applicants?|joinus|join[._-]?(our)?[._-]?team|staffing|peopleops|people|workforce|"
    r"opportunit\w*|onboarding)([._+-]\w*|\d+)?$|"
    r"^hr\w{0,3}$|"                                      # hrus@, hrdept@, hrga@
    r"(^|[._-])(hr|careers?|jobs|recruit(ing|er|ment|ers)?|talent|hiring|employment|resumes?)([._-]|$)", re.I)
IGNORE_LOCAL = re.compile(
    r"^(pr|press|media|news|newsroom|marketing|sales|support|help|helpdesk|service|services|customerservice|customer[._-]?service|"
    r"customercare|care|cs|new[._-]?work|newbusiness|new[._-]?business|privacy|legal|compliance|accounting|accounts|"
    r"accountspayable|accounts[._-]?payable|ap|ar|billing|invoices?|payables|receivables|payroll|finance|webmaster|web|"
    r"postmaster|hostmaster|abuse|security|noreply|no[._-]?reply|donotreply|do[._-]?not[._-]?reply|ir|investors?|"
    r"investor[._-]?relations|orders?|quotes?|estimates?|estimating|bids?|bidding|warranty|claims|vendors?|procurement|"
    r"purchasing|partners?|partnerships|feedback|social|events|sponsorships?|donations?|community|ethics|whistleblower|"
    r"accessibility|ada|accommodations?|eeo|dpo|gdpr|unsubscribe|bounce|mailer[._-]?daemon|reviews?|marketplace|"
    r"subcontractors?|safety|dispatch|scheduling|permits?|rfq|rfp)([._+-]\w*|\d+)?$|"
    r"accommodat|accessib|privacy|unsubscribe|no-?reply|donotreply|do-not-reply|disabilit|reasonable[._-]?adj", re.I)
GENERAL_LOCAL = re.compile(
    r"^(info|information|contact|contactus|contact[._-]us|hello|hi|hey|office|admin|administration|mail|email|inquiries|"
    r"inquiry|enquiries|enquiry|general|team|main|frontdesk|front[._-]?desk|reception|questions|ask|connect|"
    r"getintouch|letstalk|howdy|corporate|headquarters|hq)([._+-]\w*|\d+)?$", re.I)
_CTX_HIRING = re.compile(r"\b(hr|human resources|employment|recruit(ing|er|ers|ment)|r[eé]sum[eé]s?|hiring|careers|"
                         r"join (our|the) team|job (inquir\w+|openings?|opportunit\w+|seekers?)|career opportunit\w+|"
                         r"apply (for|to) (a |an |this |our )?(job|position|role|opening)s?)\b", re.I)
_CTX_ACCOM = re.compile(r"\b(accommodations?|disabilit(y|ies)|accessibility|reasonable adjustments?)\b", re.I)
_CTX_IGNORE = re.compile(r"\b(press|media inquir\w+|sales|support|privacy|billing|invoice|warranty|accommodation)\b", re.I)


def unescape_md(text: str | None) -> str:
    """JobSpy descriptions are Markdown: 'new\\_work@' -> 'new_work@'."""
    return re.sub(r"\\([_.*\-+()\[\]#!])", r"\1", text or "")


def find_emails(text: str | None) -> list[tuple[str, int]]:
    """Every address written in the text, with its position. Drops image names / placeholders."""
    out, seen = [], set()
    for m in EMAIL_RE.finditer(unescape_md(text)):
        e = m.group(0).strip(".").lower()
        local, _, dom = e.partition("@")
        if dom.rsplit(".", 1)[-1] in _ASSET_TLD or _JUNK_DOMAINS.search(dom) or re.search(r"@\d+x\.", e):
            continue
        if e not in seen:
            seen.add(e)
            out.append((e, m.start()))
    return out


def classify_email(email: str, context: str = "", from_posting: bool = False) -> tuple[str, str]:
    """-> (HIRING|GENERAL|IGNORE, reason). The mailbox name decides first; page context (a nearby
    'HR'/'Careers' label) only decides for personal/unrecognized names. In a job posting an
    unrecognized (personal) address is the hiring contact; on a company page it is GENERAL."""
    local, _, dom = (email or "").lower().partition("@")
    if not local or not dom:
        return "IGNORE", "not an email"
    if _BOARD_DOMAINS.search(dom):
        return "IGNORE", "job-board address"
    if IGNORE_LOCAL.search(local):
        return "IGNORE", f"{local}@ is not a hiring inbox"
    if HIRING_LOCAL.search(local):
        return "HIRING", f"{local}@ is an HR/careers inbox"
    if GENERAL_LOCAL.search(local):
        return "GENERAL", f"{local}@ is a general inbox"
    ctx = context or ""
    if _CTX_ACCOM.search(ctx):
        return "IGNORE", "published for accommodation/accessibility requests"
    if _CTX_HIRING.search(ctx):
        return "HIRING", f"labeled '{_CTX_HIRING.search(ctx).group(0)}' where it is published"
    if _CTX_IGNORE.search(ctx):
        return "IGNORE", f"labeled '{_CTX_IGNORE.search(ctx).group(0)}'"
    if from_posting:
        return "HIRING", "written in the job posting"
    return "GENERAL", "unlabeled address"


def _context(text: str, pos: int, before: int = 120, after: int = 40) -> str:
    return re.sub(r"\s+", " ", text[max(0, pos - before): pos + after]).strip()


def _label_context(text: str, pos: int) -> str:
    """The label a page puts on an address: its own line plus the line before (e.g.
    'Human Resources' / 'hr.dept@acme.com'), not the whole neighborhood."""
    start = text.rfind("\n", 0, pos)
    prev = text.rfind("\n", 0, max(start, 0)) if start > 0 else -1
    while prev > 0 and not text[prev:start].strip():  # skip blank lines
        start2 = prev
        prev = text.rfind("\n", 0, prev)
        start = start2
    end = text.find("\n", pos)
    seg = text[max(prev, 0): end if end != -1 else len(text)]
    return re.sub(r"\s+", " ", seg).strip()[-200:]


def posting_emails(description: str | None, jobspy_emails: str | None, source_url: str | None) -> list[dict]:
    """Classified addresses from the posting text + JobSpy's `emails` field (both read from the
    posting itself), each citing the posting URL."""
    text = unescape_md(description)
    out: dict[str, dict] = {}
    for e, pos in find_emails(text):
        cls, why = classify_email(e, _context(text, pos, 160, 30), from_posting=True)
        out[e] = {"email": e, "class": cls, "why": why, "source_url": source_url, "source": "posting",
                  "context": _context(text, pos, 80, 60)}
    for e in [x.strip().lower() for x in (jobspy_emails or "").split(",") if "@" in x]:
        if e not in out and find_emails(e):
            cls, why = classify_email(e, "", from_posting=True)
            out[e] = {"email": e, "class": cls, "why": why + " (JobSpy emails field)", "source_url": source_url,
                      "source": "posting", "context": ""}
    return list(out.values())


def best_posting_contact(description: str | None, jobspy_emails: str | None = None, source_url: str | None = None) -> str | None:
    """HIRING address written in the posting (used by followups.qualify); never GENERAL/IGNORE."""
    for e in posting_emails(description, jobspy_emails, source_url):
        if e["class"] == "HIRING":
            return e["email"]
    return None


_NAME = r"([A-Z][a-z]+(?:[- ][A-Z]\.?)?\s+[A-Z][a-zA-Z'\-]{1,30})"
_RECRUITER_RES = [
    re.compile(r"\b(?i:recruiter|talent acquisition(?: partner| specialist| manager)?|recruiting (?:partner|manager|coordinator)|"
               r"hiring manager|point of contact|contact person|contact)\s*(?i:name)?\s*[:\-–]\s*" + _NAME),
    re.compile(r"\b(?i:contact|reach out to|e-?mail|send (?:your |a )?(?:resume|cv|r[eé]sum[eé])s?(?: and cover letter)? to|"
               r"apply (?:directly )?(?:with|to)|questions\?? (?:contact|email))\s+" + _NAME + r"\s*(?:at|@|via|\(|<|,|-|–)"),
    re.compile(_NAME + r"\s*[,|\-–]\s*(?:Senior |Sr\.? |Lead |Technical |Executive )?(?:Recruiter|Talent Acquisition|Recruiting)"),
]
_NOT_NAMES = {"human resources", "talent acquisition", "hiring manager", "equal opportunity", "united states", "job description",
              "the company", "our team", "please note", "apply now", "project manager", "full time", "job type"}


def recruiter_name(description: str | None) -> str | None:
    text = re.sub(r"[*_`#>]+", " ", unescape_md(description))
    text = re.sub(r"[ \t]+", " ", text)
    for rx in _RECRUITER_RES:
        for m in rx.finditer(text):
            name = m.group(1).strip()
            if name.lower() not in _NOT_NAMES and not re.search(r"\b(The|Our|This|Your|We|Team|Inc|LLC|Company|Group)\b", name):
                return name
    return None


# ----------------------------------------------------------------------------- domains / names
AGGREGATOR_HOSTS = re.compile(
    r"(^|\.)(indeed|glassdoor|linkedin|ziprecruiter|monster|careerbuilder|simplyhired|google|facebook|fb|instagram|twitter|x|"
    r"youtube|tiktok|pinterest|yelp|bbb|mapquest|wikipedia|bloomberg|zoominfo|crunchbase|dnb|manta|yellowpages|angi|angieslist|"
    r"homeadvisor|houzz|thumbtack|nextdoor|apple|bing|duckduckgo|builtin|builtinatlanta|comparably|salary|payscale|ripoffreport|"
    r"opencorporates|bizapedia|buzzfile|rocketreach|signalhire|contactout|apollo|lusha|levels|teal|tealhq|lensa|jooble|talent|"
    r"jobright|dice|wellfound|reddit|quora|rooferbutler|porch|buildzoom|sec|prnewswire|businesswire|globenewswire|chamberofcommerce|"
    r"birdeye|trustpilot|indeedjobs|jobs2careers|adzuna|neuvoo|careerjet|snagajob|workingnotworking|theladders|ladders|"
    r"themuse|vault|owler|cbinsights|pitchbook|craft|allbiz|dandb|hoovers|corporationwiki|govtribe|nicelocal|cylex|merchantcircle|"
    r"superpages|whitepages|spokeo|zillow|clutch|upcity|expertise|threebestrated|guildquality|gaf|owenscorning|certainteed|"
    r"amazon|github|medium|substack|wordpress|blogspot|issuu|scribd|slideshare|yahoo|msn|aol|archive|weebly)\.[a-z.]+$", re.I)
ATS_HOSTS = re.compile(
    r"(^|\.)(greenhouse\.io|grnh\.se|lever\.co|ashbyhq\.com|myworkdayjobs\.com|workday\.com|icims\.com|paylocity\.com|"
    r"adp\.com|bamboohr\.com|applytojob\.com|jazzhr\.com|breezy\.hr|paycomonline\.net|paycom\.com|ultipro\.com|ukg\.net|"
    r"rec\.pro\.ukg\.net|smartrecruiters\.com|jobvite\.com|workable\.com|recruitee\.com|rippling\.com|dayforcehcm\.com|"
    r"taleo\.net|successfactors\.com|successfactors\.eu|oraclecloud\.com|teamtailor\.com|pinpointhq\.com|jobs\.personio\.\w+|"
    r"hirebridge\.com|isolvedhire\.com|recruiting\.paylocity\.com|apply\.workable\.com|careers-page\.com|hrmdirect\.com|"
    r"clearcompany\.com|applicantpro\.com|applicantstack\.com|trinethire\.com|hire\.trakstar\.com|zohorecruit\.com|"
    r"gusto\.com|bullhornstaffing\.com|avature\.net|phenompeople\.com|eightfold\.ai|ziprecruiter\.com/c/)", re.I)
WEBMAIL = re.compile(r"^(gmail|yahoo|outlook|hotmail|aol|icloud|me|msn|live|comcast|att|bellsouth|protonmail|proton|ymail|"
                     r"mail|gmx|zoho)\.", re.I)
_GENERIC_TOKENS = {"construction", "roofing", "services", "service", "solutions", "systems", "industries", "industry", "builders",
                   "building", "contractors", "contracting", "exteriors", "interiors", "home", "homes", "restoration", "remodeling",
                   "repairs", "maintenance", "management", "consulting", "partners", "associates", "enterprises", "international",
                   "global", "national", "america", "american", "usa", "us", "atlanta", "georgia", "south", "southeast", "southern",
                   "north", "east", "west", "plant", "llc", "inc", "corp", "group", "company", "the", "and", "of", "technologies",
                   "technology", "tech", "labs", "staffing", "resources", "energy", "electric", "mechanical", "plumbing", "hvac",
                   "facilities", "facility", "properties", "property", "realty", "capital", "health", "healthcare", "care",
                   "medical", "logistics", "transport", "transportation", "co", "general", "commercial", "residential"}
_ANON_FIRST = {"multi", "reputable", "leading", "growing", "established", "local", "national", "large", "regional", "top",
               "premier", "prestigious", "well", "fast", "family", "small", "mid", "booming", "thriving", "successful", "award",
               "respected", "busy", "expanding", "innovative", "privately", "private", "confidential", "nationwide", "reliable",
               "trusted", "major", "dynamic", "highly", "growing", "elite", "industry", "an", "a", "our", "fortune", "prominent",
               "stable", "rapidly", "full", "high", "quality", "commercial", "residential", "veteran", "locally"}
_ANON_LAST = {"company", "firm", "employer", "client", "contractor", "business", "organization", "corporation", "agency",
              "provider", "manufacturer", "builder", "company."}
_ANON_RE = re.compile(r"^\s*(confidential|anonymous|undisclosed|withheld|company confidential|confidential company|"
                      r"private (company|client|employer)|hiring company|our client|a client|client of|staffing agency|"
                      r"recruiting agency|n/?a|unknown|employer)\b", re.I)


def is_anonymous(name: str | None) -> bool:
    """'Multi Regional Roofing Company', 'Confidential', 'Reputable Repairs and Maintanence Company'."""
    n = (name or "").strip()
    if not n or _ANON_RE.search(n):
        return True
    words = re.findall(r"[a-z]+", n.lower())
    return len(words) >= 3 and words[0] in _ANON_FIRST and words[-1] in _ANON_LAST


def company_key(name: str | None) -> str:
    return norm_company(name) or re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def host_of(url: str | None) -> str:
    try:
        h = (urlparse(url if "://" in (url or "") else f"https://{url}").hostname or "").lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def reg_domain(host: str) -> str:
    """foo.bar.co.uk -> bar.co.uk ; careers.acme.com -> acme.com (no public-suffix list; good enough)."""
    parts = [p for p in (host or "").lower().split(".") if p]
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in ("co", "com", "org", "net", "gov", "ac", "edu", "ltd", "plc"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _label(host: str) -> str:
    return re.sub(r"[^a-z0-9]", "", reg_domain(host).split(".")[0])


def _tokens(name: str) -> list[str]:
    return [t for t in company_key(name).split() if t]


_DOMAIN_SUFFIXES = {"llc", "inc", "co", "corp", "usa", "us", "group", "grp", "hq", "online", "home", "ltd", "company", "companies",
                    "ga", "atl", "atlanta", "global", "intl", "web", "site", "net"}


def name_match_score(name: str, host_or_slug: str, is_slug: bool = False) -> int:
    """How well a domain label (or LinkedIn slug) matches a company name. 3 = clearly it,
    2 = very likely, 1 = weak, 0 = no."""
    label = re.sub(r"[^a-z0-9]", "", host_or_slug.lower()) if is_slug else _label(host_or_slug)
    toks = _tokens(name)
    if not label or not toks:
        return 0
    concat = "".join(toks)
    raw = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    if label in (concat, raw) or (len(concat) >= 3 and concat in label) or (len(label) >= 5 and concat.startswith(label)):
        return 3  # foreverext.com for "FOREVER Exteriors" (prefix), not georgia.gov for "...System of Georgia"
    distinct = [t for t in toks if t not in _GENERIC_TOKENS and len(t) >= 3]
    if distinct and label == distinct[0]:
        return 3  # dpr.com for "DPR Construction"
    initials = "".join(t[0] for t in toks if t not in ("of", "and", "for", "at", "in"))
    if len(initials) >= 3 and label.startswith(initials) and len(label) <= len(initials) + 6:
        return 2
    if distinct and label.startswith(distinct[0]):
        rest = label[len(distinct[0]):]  # roystonllc -> "llc", foreverext -> "ext"; priceline -> "line" (no)
        if rest in _DOMAIN_SUFFIXES or any(t != distinct[0] and len(rest) >= 2 and (t.startswith(rest) or rest.startswith(t))
                                           for t in toks):
            return 2
    hits = [t for t in distinct if t in label]
    if len(hits) >= 2:
        return 2
    return 1 if hits and len(hits[0]) >= 4 else 0


def alt_names(name: str, jobs: list[dict]) -> list[str]:
    """Legal names the posting itself uses, e.g. 'Royston LLC' for the board name 'Royston Plant'."""
    toks = [t for t in _tokens(name) if t not in _GENERIC_TOKENS and len(t) >= 3]
    if not toks:
        return []
    rx = re.compile(r"\b(" + re.escape(toks[0]) + r"(?:[ &'.-]+[A-Z][\w&'.-]*){0,3}?,?\s+(?:LLC|L\.L\.C\.|Inc\.?|Corporation|Corp\.?|"
                    r"Company|Co\.|Ltd\.?|Group))\b", re.I)
    out = []
    for j in jobs[:5]:
        for m in rx.finditer(unescape_md(j.get("description"))[:6000]):
            a = re.sub(r"\s+", " ", m.group(1)).strip(" ,.")
            if company_key(a) != company_key(name) or a.lower() != name.lower():
                out.append(a)
    return list(dict.fromkeys(out))[:2]


def _plain(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def probe_name_domain(f: "Fetcher", names: list[str]) -> str | None:
    """'Royston LLC' -> https://roystonllc.com, accepted ONLY if that site's own <title>/site name
    contains the full name. A domain check, not an email guess."""
    from bs4 import BeautifulSoup

    for n in names:
        raw = re.sub(r"[^a-z0-9]", "", n.lower())
        if len(raw) < 6 or len(_plain(n).split()) < 2:
            continue
        fu, html = f.get(f"https://{raw}.com")
        if not html:
            continue
        soup = BeautifulSoup(html[:200_000], "lxml")
        t = " ".join(filter(None, [soup.title.get_text(" ") if soup.title else "",
                                   (soup.find("meta", attrs={"property": "og:site_name"}) or {}).get("content", "")]))
        if _plain(n) in _plain(t):
            return f"https://{host_of(fu)}"
    return None


# ----------------------------------------------------------------------------- HTTP
class Fetcher:
    """Polite requests session: UA, timeouts, robots.txt, per-host delay, size cap."""

    def __init__(self, ecfg: dict):
        import requests

        self.s = requests.Session()
        self.s.headers.update({"User-Agent": ecfg.get("user_agent") or "job-aggregator-enrich/1.0",
                               "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.5",
                               "Accept-Language": "en-US,en;q=0.8"})
        self.timeout = float(ecfg.get("request_timeout_seconds") or 10)
        self.delay = float(ecfg.get("delay_seconds") or 0.6)
        self._last: dict[str, float] = {}
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self.requests = 0
        self.last_status: int | str | None = None

    def _wait(self, host: str):
        dt = time.time() - self._last.get(host, 0)
        if dt < self.delay:
            time.sleep(self.delay - dt)
        self._last[host] = time.time()

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp = None
            try:
                self._wait(p.netloc)
                self.requests += 1
                r = self.s.get(base + "/robots.txt", timeout=self.timeout)
                if r.status_code == 200 and len(r.content) < 500_000:
                    rp = robotparser.RobotFileParser()
                    rp.parse(r.text.splitlines())
            except Exception:  # noqa: BLE001 - unreachable robots.txt = no rules
                rp = None
            self._robots[base] = rp
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch(self.s.headers["User-Agent"], url)

    def get(self, url: str, json_ok: bool = False, check_robots: bool = True):
        """-> (final_url, text) or (None, None). Never raises."""
        try:
            if check_robots and not self.allowed(url):
                log.debug("robots.txt disallows %s", url)
                self.last_status = "robots.txt"
                return None, None
            self._wait(urlparse(url).netloc)
            self.requests += 1
            r = self.s.get(url, timeout=self.timeout, allow_redirects=True, stream=True)
            ctype = r.headers.get("content-type", "")
            self.last_status = r.status_code
            if r.status_code != 200 or not ("html" in ctype or (json_ok and "json" in ctype) or not ctype):
                r.close()
                return None, None
            body = r.raw.read(2_000_000, decode_content=True)
            r.close()
            return r.url, body.decode(r.encoding or "utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            log.debug("GET %s failed: %s", url, e)
            self.last_status = type(e).__name__
            return None, None


# ----------------------------------------------------------------------------- page parsing
_CAREER_TXT = re.compile(r"\b(careers?|jobs?|join (our|the) team|employment|work (with|for) us|we'?re hiring|now hiring|"
                         r"opportunities|hiring|open positions)\b", re.I)
_CAREER_HREF = re.compile(r"(career|/jobs?\b|/jobs?/|employment|join-?(our-?)?team|work-?with-?us|hiring|opportunit|open-?positions)", re.I)
_CONTACT_HREF = re.compile(r"(contact|get-?in-?touch|locations?$|/locations?/)", re.I)
_ABOUT_HREF = re.compile(r"(about|who-?we-?are|our-?(story|company))", re.I)
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?([2-9]\d{2})\)?[\s.-]?([2-9]\d{2})[\s.-](\d{4})(?!\d)")


def _cf_decode(hexstr: str) -> str | None:
    """Cloudflare 'email protection' obfuscation of an address that IS published on the page."""
    try:
        key = int(hexstr[:2], 16)
        return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr), 2))
    except ValueError:
        return None


def parse_page(url: str, html: str) -> dict:
    """Emails (with context), links, phones, meta description, JSON-LD org data from one page."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    out = {"url": url, "emails": [], "links": [], "phones": [], "description": None, "ld": {}, "iframes": []}
    for t in soup(["script", "style", "noscript", "template"]):
        if t.name == "script" and "ld+json" in (t.get("type") or ""):
            try:
                data = json.loads(t.string or "")
                for d in data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []:
                    if isinstance(d, dict) and str(d.get("@type", "")).lower() in (
                            "organization", "localbusiness", "corporation", "homeandconstructionbusiness", "roofingcontractor",
                            "generalcontractor", "professionalservice", "corporation"):
                        out["ld"] = d
                        break
            except Exception:  # noqa: BLE001
                pass
        if t.name in ("script",) and t.get("src"):
            out["iframes"].append(urljoin(url, t["src"]))
        t.decompose()
    for fr in soup.find_all(["iframe", "embed"]):
        if fr.get("src"):
            out["iframes"].append(urljoin(url, fr["src"]))
    m = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    if m and m.get("content"):
        out["description"] = re.sub(r"\s+", " ", m["content"]).strip()
    seen: set[str] = set()
    for el in soup.select("[data-cfemail]"):
        found = find_emails(_cf_decode(el.get("data-cfemail", "")) or "")
        if found and found[0][0] not in seen:
            e = found[0][0]
            ctx = re.sub(r"\s+", " ", (el.parent.get_text(" ") if el.parent else ""))[:200]
            out["emails"].append({"email": e, "context": ctx, "how": "cloudflare-protected"})
            seen.add(e)
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = re.sub(r"\s+", " ", a.get_text(" ")).strip()
        if href.lower().startswith("mailto:"):
            addr = href[7:].split("?")[0].strip()
            for e, _ in find_emails(addr):
                if e not in seen:
                    seen.add(e)
                    parent = a.find_parent(["p", "li", "td", "dd", "div"]) or a.parent
                    ptxt = re.sub(r"\s+", " ", parent.get_text(" ") if parent else text)
                    ctx = ptxt[:200] if len(ptxt) <= 300 else text  # a huge parent (whole footer) is no label
                    out["emails"].append({"email": e, "context": ctx, "how": "mailto"})
        elif href.lower().startswith("tel:"):
            out["phones"].append(re.sub(r"[^\d+]", "", href[4:]))
        elif "/cdn-cgi/l/email-protection#" in href:
            found = find_emails(_cf_decode(href.split("#", 1)[1]) or "")
            if found and found[0][0] not in seen:
                seen.add(found[0][0])
                out["emails"].append({"email": found[0][0], "context": text[:200], "how": "cloudflare-protected"})
        elif not href.startswith(("#", "javascript:")):
            out["links"].append((urljoin(url, href), text[:80]))
    text = re.sub(r"[ \t\r\f\v]+", " ", soup.get_text("\n"))
    for e, pos in find_emails(text):
        if e not in seen:
            seen.add(e)
            out["emails"].append({"email": e, "context": _label_context(text, pos), "how": "text"})
    if not out["phones"]:
        for m in _PHONE_RE.finditer(text):
            out["phones"].append("".join(m.groups()))
            if len(out["phones"]) >= 3:
                break
    return out


def fmt_phone(p: str) -> str:
    d = re.sub(r"\D", "", p or "")
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if len(d) == 10 else (p or "")


def _one_line(s: str | None, n: int = 180) -> str | None:
    s = re.sub(r"\s+", " ", unescape_md(s or "")).strip(" -*#")
    if not s:
        return None
    m = re.match(r"(.{40,%d}?[.!?])(\s|$)" % n, s)
    s = m.group(1) if m else s
    return s if len(s) <= n else s[: n - 1].rsplit(" ", 1)[0] + "…"


# ----------------------------------------------------------------------------- ATS
ATS_API = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
}


def ats_board_of(url: str | None) -> tuple[str, str] | None:
    """(ats, slug) for Greenhouse/Lever/Ashby board or embed URLs."""
    if not url:
        return None
    p = urlparse(url)
    h, parts = (p.hostname or "").lower(), [x for x in p.path.split("/") if x]
    q = parse_qs(p.query)
    if "greenhouse.io" in h:
        if q.get("for"):
            return "greenhouse", q["for"][0]
        if parts and parts[0] not in ("embed", "v1"):
            return "greenhouse", parts[0]
    if h in ("jobs.lever.co", "jobs.eu.lever.co") and parts:
        return "lever", parts[0]
    if h == "jobs.ashbyhq.com" and parts:
        return "ashby", parts[0]
    return None


def _title_sim(a: str, b: str) -> float:
    ta = set(re.findall(r"[a-z0-9]+", (a or "").lower())) - {"the", "and", "of", "a", "an", "for", "to", "in", "at", "with", "ii", "i"}
    tb = set(re.findall(r"[a-z0-9]+", (b or "").lower())) - {"the", "and", "of", "a", "an", "for", "to", "in", "at", "with", "ii", "i"}
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def match_on_ats(f: Fetcher, ats: str, slug: str, title: str) -> str | None:
    """Same job on the company's own Greenhouse/Lever/Ashby board -> its apply URL."""
    _, body = f.get(ATS_API[ats].format(slug=slug), json_ok=True, check_robots=False)
    if not body:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    posts = data.get("jobs") if isinstance(data, dict) else data
    best, best_s = None, 0.0
    for j in posts or []:
        t = j.get("title") or j.get("text") or ""
        s = _title_sim(title, t)
        if s > best_s:
            best, best_s = j, s
    if best and best_s >= 0.6:
        return best.get("absolute_url") or best.get("hostedUrl") or best.get("jobUrl") or best.get("applyUrl")
    return None


# ----------------------------------------------------------------------------- board details
def fetch_board_details(job: dict) -> dict:
    """LinkedIn/ZipRecruiter/Glassdoor row saved without a description: fetch that one posting
    via JobSpy's own scraper (same requests JobSpy makes with fetch_description=True)."""
    src, url = job.get("source"), job.get("url") or ""
    from types import SimpleNamespace

    from jobspy.model import Country, DescriptionFormat
    from jobspy.util import create_session

    si = SimpleNamespace(description_format=DescriptionFormat.MARKDOWN, country=Country.USA)
    if src == "linkedin":
        from jobspy.linkedin import LinkedIn

        m = re.search(r"/jobs/view/(?:[^/?]*-)?(\d+)", url)
        if not m:
            return {}
        s = LinkedIn()
        s.scraper_input = si
        d = s._get_job_details(m.group(1))  # noqa: SLF001
        return {k: v for k, v in {"description": d.get("description"), "company_industry": d.get("company_industry"),
                                  "job_level": d.get("job_level"), "job_function": d.get("job_function"),
                                  "company_logo": d.get("company_logo")}.items() if isinstance(v, str) and v}
    if src == "zip_recruiter":
        from jobspy.ziprecruiter import ZipRecruiter

        z = ZipRecruiter()
        z.scraper_input = si
        z.session = create_session()
        z.session.impersonate = "safari"
        d = z._fetch_details(url)  # noqa: SLF001
        return {k: v for k, v in d.items() if isinstance(v, str) and v}
    if src == "glassdoor":
        from jobspy.glassdoor import Glassdoor
        from jobspy.glassdoor.constant import headers

        m = re.search(r"jl=(\d+)", url)
        if not m:
            return {}
        g = Glassdoor()
        g.scraper_input = si
        g.base_url = Country.USA.get_glassdoor_url()
        g.session = create_session()
        g.session.headers.update(headers)
        g.session.get(g._autocomplete_url("Atlanta"), timeout=15)  # noqa: SLF001 - sets the cookies the API needs
        desc = g._fetch_job_description(m.group(1))  # noqa: SLF001
        return {"description": desc} if isinstance(desc, str) and desc else {}
    return {}


# ----------------------------------------------------------------------------- search
class Searcher:
    """DuckDuckGo via the `ddgs` package (no key). Slow-paced; DuckDuckGo sometimes answers a burst
    with an HTTP 202 challenge, so one retry goes through ddgs' 'auto' backend (also keyless)."""

    def __init__(self, ecfg: dict):
        self.enabled = bool(ecfg.get("search", True))
        self.blocked = False
        self.calls = 0
        self.fails = 0
        self._last = 0.0

    def _pace(self, gap: float):
        dt = time.time() - self._last
        if dt < gap:
            time.sleep(gap - dt)
        self._last = time.time()

    def text(self, query: str, n: int = 8) -> list[dict]:
        if not self.enabled or self.blocked:
            return []
        try:
            from ddgs import DDGS
        except ImportError:
            log.warning("ddgs not installed; skipping web search (pip install -r requirements.txt)")
            self.enabled = False
            return []
        for backend, gap in (("duckduckgo", 2.5), ("auto", 4.0)):
            self._pace(gap)
            self.calls += 1
            try:
                res = DDGS(timeout=10).text(query, region="us-en", max_results=n, backend=backend) or []
                if res:
                    self.fails = 0
                    return res
            except Exception as e:  # noqa: BLE001 - "No results found" / challenge / timeout
                log.debug("search %r via %s: %s", query, backend, e)
        self.fails += 1
        if self.fails >= 4:
            log.warning("web search keeps failing (rate limit?); no more searches this run")
            self.blocked = True
        return []


def _city_state(loc: str | None) -> str:
    loc = re.sub(r"\(.*?\)", "", loc or "")
    if re.search(r"remote|anywhere|worldwide", loc, re.I):
        return ""
    parts = [p.strip() for p in loc.split(",") if p.strip() and p.strip().upper() not in ("US", "USA", "UNITED STATES")]
    return ", ".join(parts[:2])


_US_LOC = re.compile(r",\s*[A-Z]{2}\b|united states|\busa?\b|georgia|remote", re.I)
_OK_CCTLD = {"us", "co", "io", "ai", "me", "tv", "ly", "so", "to", "gg", "cc", "fm"}


def search_company(searcher: Searcher, name: str, location: str | None, alt_names: list[str] | None = None) -> dict:
    """-> {website, linkedin_url, query} from DuckDuckGo results (best name-matching domain)."""
    out: dict = {}
    where = _city_state(location)
    us_job = bool(_US_LOC.search(location or "")) or not location
    queries = [(f"{a} {where}".strip(), a) for a in (alt_names or [])] + [(f"{name} {where}".strip(), name)]
    if where:
        queries.append((name, name))
    out["results"] = 0
    for q, qname in dict.fromkeys(queries):
        res = searcher.text(q)
        out["results"] += len(res)
        cands = []
        for i, r in enumerate(res):
            href = r.get("href") or ""
            h = host_of(href)
            if not h:
                continue
            m = re.match(r"https?://([a-z]+\.)?linkedin\.com/company/([^/?#]+)", href)
            if m and not out.get("linkedin_url") and name_match_score(name, m.group(2), is_slug=True) >= 2:
                out["linkedin_url"] = f"https://www.linkedin.com/company/{m.group(2)}"
            if AGGREGATOR_HOSTS.search(h) or ATS_HOSTS.search(h):
                continue
            tld = h.rsplit(".", 1)[-1]
            if us_job and len(tld) == 2 and tld not in _OK_CCTLD:
                continue  # e.g. royston.co.uk for a job in Jasper, GA
            sc = max(name_match_score(name, h), name_match_score(qname, h))
            title = company_key(r.get("title") or "")
            if sc == 1 and company_key(qname) and company_key(qname) in title:
                sc = 2
            raw = re.sub(r"[^a-z0-9]", "", qname.lower())
            bonus = len(_label(h)) if raw and _label(h) and _label(h) in raw else 0  # prefer the longest exact-name domain
            if sc >= 2:
                cands.append((sc, bonus, -i, h))
        if cands:
            sc, _, _, h = max(cands)
            out.update(website=f"https://{reg_domain(h)}", query=q)
            break
    return out


# ----------------------------------------------------------------------------- company crawl
def crawl_company(f: Fetcher, website: str, name: str, max_pages: int) -> dict:
    """Home + careers/jobs/contact/about pages on the company's own domain."""
    res = {"pages": [], "emails": [], "phones": [], "careers_url": None, "ats_board_url": None, "linkedin_url": None,
           "description": None, "hq": None, "website": website, "phone_source": None}
    final, html = f.get(website)
    st = f.last_status
    h0 = host_of(website)
    for alt in ([f"https://www.{h0}"] if not h0.startswith("www.") else []) + [f"http://{h0}"]:
        if html or not isinstance(st, str) or st == "robots.txt":
            break  # only retry on connection/TLS errors, never around an HTTP 403/404
        final, html = f.get(alt)
        st = st if not html and isinstance(f.last_status, str) else f.last_status
    if not html:
        res["error"] = ("site refuses automated requests (HTTP 403); check it by hand" if st == 403 else
                        "robots.txt disallows crawling" if st == "robots.txt" else f"homepage unreachable ({st})")
        return res
    home_dom = reg_domain(host_of(final))
    if home_dom != reg_domain(host_of(website)) and name_match_score(name, host_of(final)) >= 2:
        res["website"] = f"https://{host_of(final)}"  # redirected to the company's real domain
    dom = reg_domain(host_of(res["website"]))
    pages = [parse_page(final, html)]
    home = pages[0]
    res["pages"].append(final)

    def own(u):
        return reg_domain(host_of(u)) == dom

    careers, contact, about = [], [], []
    for u, t in home["links"]:
        uh = host_of(u)
        if re.match(r"https?://([a-z]+\.)?linkedin\.com/company/", u) and not res["linkedin_url"]:
            res["linkedin_url"] = u.split("?")[0].rstrip("/")
        if ATS_HOSTS.search(uh) and (_CAREER_TXT.search(t) or _CAREER_HREF.search(u)) and not res["ats_board_url"]:
            res["ats_board_url"] = u
        if not own(u):
            continue
        path = urlparse(u).path.lower()
        if _CAREER_TXT.search(t) or _CAREER_HREF.search(path):
            careers.append(u.split("#")[0])
        elif _CONTACT_HREF.search(path) or re.search(r"\bcontact\b", t, re.I):
            contact.append(u.split("#")[0])
        elif _ABOUT_HREF.search(path) or re.search(r"\babout\b", t, re.I):
            about.append(u.split("#")[0])
    root = f"{urlparse(final).scheme}://{urlparse(final).netloc}"
    plan = (list(dict.fromkeys(careers))[:2] or [root + "/careers", root + "/jobs"]) \
        + (list(dict.fromkeys(contact))[:2] or [root + "/contact", root + "/contact-us"]) \
        + (list(dict.fromkeys(about))[:1] or [root + "/about"])
    for u in dict.fromkeys(plan):
        if len(res["pages"]) >= max_pages:
            break
        if u.rstrip("/") == final.rstrip("/"):
            continue
        fu, body = f.get(u)
        if not body or fu in res["pages"]:
            continue
        pg = parse_page(fu, body)
        pages.append(pg)
        res["pages"].append(fu)
        is_careers = bool(_CAREER_HREF.search(urlparse(fu).path) or _CAREER_HREF.search(u))
        if is_careers and not res["careers_url"]:
            res["careers_url"] = fu
        for lu, t in pg["links"]:
            if ATS_HOSTS.search(host_of(lu)) and not res["ats_board_url"] and (is_careers or _CAREER_TXT.search(t)):
                res["ats_board_url"] = lu
            if re.match(r"https?://([a-z]+\.)?linkedin\.com/company/", lu) and not res["linkedin_url"]:
                res["linkedin_url"] = lu.split("?")[0].rstrip("/")
        if is_careers and not res["ats_board_url"]:
            for src in pg["iframes"]:
                if ATS_HOSTS.search(host_of(src)):
                    res["ats_board_url"] = src
                    break
    if careers and not res["careers_url"]:
        res["careers_url"] = careers[0]
    # emails: only on the company's own domain, each citing the page it was read from
    rank = {"HIRING": 0, "GENERAL": 1, "IGNORE": 2}
    best: dict[str, dict] = {}
    for pg in pages:
        for e in pg["emails"]:
            edom = reg_domain(e["email"].split("@", 1)[1])
            cls, why = classify_email(e["email"], e["context"])
            if edom != dom:
                cls, why = "IGNORE", f"not on the company domain ({dom})"
            item = {"email": e["email"], "class": cls, "why": why, "source_url": pg["url"],
                    "source": "website", "how": e["how"], "context": e["context"][:160]}
            old = best.get(e["email"])
            if old is None or rank[cls] < rank[old["class"]]:
                best[e["email"]] = item  # same address on several pages: keep the page that labels it best
    res["emails"] = list(best.values())
    for pg in [p for p in pages if re.search(r"contact", p["url"], re.I)] + pages:
        if pg["phones"]:
            res["phone"], res["phone_source"] = fmt_phone(pg["phones"][0]), pg["url"]
            break
    ld = home.get("ld") or {}
    if isinstance(ld.get("address"), dict):
        a = ld["address"]
        res["hq"] = ", ".join(str(a.get(k)) for k in ("streetAddress", "addressLocality", "addressRegion") if a.get(k)) or None
    if not res.get("phone") and ld.get("telephone"):
        res["phone"], res["phone_source"] = fmt_phone(str(ld["telephone"])), final
    for s in ld.get("sameAs") or []:
        if isinstance(s, str) and "linkedin.com/company/" in s and not res["linkedin_url"]:
            res["linkedin_url"] = s.split("?")[0].rstrip("/")
    res["description"] = _one_line(home.get("description") or ld.get("description"))
    return res


# ----------------------------------------------------------------------------- orchestration
def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _loads(s, default=None):
    try:
        return json.loads(s) if s else (default if default is not None else [])
    except ValueError:
        return default if default is not None else []


def _good_direct(url: str | None) -> bool:
    h = host_of(url)
    return bool(h) and not AGGREGATOR_HOSTS.search(h)


def _merge_emails(*lists) -> list[dict]:
    out: dict[str, dict] = {}
    for lst in lists:
        for e in lst or []:
            out.setdefault(e["email"], e)
    return list(out.values())


class Enricher:
    def __init__(self, conn, cfg: dict, budget_s: float | None = None, max_companies: int | None = None, force: bool = False):
        self.conn, self.cfg = conn, cfg
        self.e = cfg.get("enrich") or {}
        self.f = Fetcher(self.e)
        self.search = Searcher(self.e)
        self.t0 = time.time()
        self.budget = float(budget_s if budget_s is not None else self.e.get("max_seconds_per_run") or 270)
        self.max_companies = int(max_companies if max_companies is not None else self.e.get("max_companies_per_run") or 25)
        self.force = force
        self.companies_crawled = 0
        self._done: set[str] = set()
        self.stats = {"jobs": 0, "companies_crawled": 0, "companies_cached": 0, "anonymous": 0, "details_fetched": 0,
                      "contacts_set": [], "errors": [], "deferred": 0}

    def over_budget(self) -> bool:
        return time.time() - self.t0 > self.budget

    # -- company row
    def _company_row(self, key: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM companies WHERE company_key=?", (key,)).fetchone()
        return dict(r) if r else None

    def _fresh(self, row: dict | None) -> bool:
        if not row or not row.get("last_enriched_at") or self.force:
            return False
        try:
            when = datetime.fromisoformat(row["last_enriched_at"])
        except ValueError:
            return False
        return datetime.now(timezone.utc) - when.astimezone(timezone.utc) < timedelta(days=float(self.e.get("cache_days") or 30))

    def _save_company(self, c: dict):
        cols = ["company_key", "name", "status", "website", "domain", "website_source", "careers_url", "ats_board_url",
                "hiring_emails", "general_emails", "ignored_emails", "phone", "phone_source", "hq", "description", "industry",
                "size", "linkedin_url", "pages_fetched", "notes", "last_enriched_at"]
        vals = [c.get(k) for k in cols]
        with self.conn:
            self.conn.execute(f"INSERT INTO companies ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
                              f"ON CONFLICT (company_key) DO UPDATE SET "
                              + ",".join(f"{k}=excluded.{k}" for k in cols[1:]), vals)

    def enrich_company(self, job: dict, siblings: list[dict]) -> dict:
        """Company-level info from the job rows (JobSpy fields) + search + own-site crawl."""
        name = (job.get("company") or "").strip()
        key = company_key(name)
        row = self._company_row(key)
        if key in self._done and row:
            return row  # already looked up during this run (even with --force)
        self._done.add(key)
        if self._fresh(row):
            self.stats["companies_cached"] += 1
            return row
        c = {"company_key": key, "name": name, "status": "ok", "notes": None}
        jobs = [job] + siblings
        pick = lambda col: next((j.get(col) for j in jobs if j.get(col)), None)  # noqa: E731
        c["industry"] = pick("company_industry")
        c["size"] = pick("company_num_employees")
        c["hq"] = pick("company_addresses")
        c["description"] = _one_line(pick("company_description"))
        li = next((j["company_url"] for j in jobs if "linkedin.com/company/" in (j.get("company_url") or "")), None)
        c["linkedin_url"] = li.split("?")[0].rstrip("/") if li else None
        if is_anonymous(name):
            c.update(status="anonymous", notes="anonymous listing (employer name withheld by the board); no web lookup",
                     last_enriched_at=_now())
            self.stats["anonymous"] += 1
            self._save_company(c)
            return c
        # website: JobSpy employer website > ATS posting on own domain > posting text > search
        site, how = None, None
        for j in jobs:
            u = j.get("company_url_direct")
            if u and _good_direct(u) and not ATS_HOSTS.search(host_of(u)):
                site, how = u, "jobspy"
                break
        if not site:
            for j in jobs:
                if j.get("source") in ("greenhouse", "lever", "ashby") and j.get("url"):
                    h = host_of(j["url"])
                    if not ATS_HOSTS.search(h) and name_match_score(name, h) >= 2:
                        site, how = f"https://{h}", "ats"
                        break
        if not site:
            for j in jobs:
                text = unescape_md(j.get("description"))
                hosts = [host_of(u) for u in re.findall(r"(?:https?://|www\.)[A-Za-z0-9.-]+\.[a-z]{2,}", text)]
                hosts += [e.split("@", 1)[1] for e, _ in find_emails(text)]
                for h in hosts:
                    if h and not AGGREGATOR_HOSTS.search(h) and not ATS_HOSTS.search(h) and not WEBMAIL.match(h) \
                            and name_match_score(name, h) >= 2:
                        site, how = f"https://{reg_domain(h)}", "posting"
                        break
                if site:
                    break
        alts = alt_names(name, jobs)
        if self.companies_crawled >= self.max_companies or self.over_budget():
            c.update(status="partial", website=site, domain=reg_domain(host_of(site)) if site else None, website_source=how,
                     notes="web lookup deferred (per-run cap reached)")
            self.stats["deferred"] += 1
            self._save_company({**(row or {}), **{k: v for k, v in c.items() if v is not None}, "last_enriched_at": None})
            return c
        self.companies_crawled += 1
        self.stats["companies_crawled"] += 1
        loc = job.get("location")
        if not site:  # the legal name the posting uses, as a .com whose own title carries that name
            site = probe_name_domain(self.f, alts + ([name] if len(name.split()) >= 2 else []))
            how = "name.com (title matches)" if site else None
        found: dict = {}
        if not site or not c["linkedin_url"]:
            found = search_company(self.search, name, loc, alts) if self.search.enabled else {}
            if not site and found.get("website"):
                site, how = found["website"], "search"
            c["linkedin_url"] = c["linkedin_url"] or found.get("linkedin_url")
        if not site:
            searched_ok = bool(found.get("results"))
            c.update(status="not_found",
                     notes="no company website found (JobSpy, posting, search)" if searched_ok
                     else "no website from JobSpy/posting and web search unavailable; will retry next run",
                     last_enriched_at=_now() if searched_ok else None)
            self._save_company(c)
            return c
        site = site if "://" in site else "https://" + site
        site = f"{urlparse(site).scheme}://{urlparse(site).netloc}"
        cr = crawl_company(self.f, site, name, int(self.e.get("max_pages_per_company") or 8))
        emails = cr["emails"]
        c.update(website=cr["website"], domain=reg_domain(host_of(cr["website"])), website_source=how,
                 careers_url=cr["careers_url"], ats_board_url=cr["ats_board_url"],
                 hiring_emails=json.dumps([e for e in emails if e["class"] == "HIRING"]),
                 general_emails=json.dumps([e for e in emails if e["class"] == "GENERAL"]),
                 ignored_emails=json.dumps([e for e in emails if e["class"] == "IGNORE"]),
                 phone=cr.get("phone"), phone_source=cr.get("phone_source"),
                 hq=c["hq"] or cr.get("hq"), description=c["description"] or cr.get("description"),
                 linkedin_url=c["linkedin_url"] or cr.get("linkedin_url"),
                 pages_fetched=json.dumps(cr["pages"]), last_enriched_at=_now())
        log.info("company %-32s %s (via %s): %d page(s), %d hiring / %d general email(s)%s", name[:32], c["website"], how,
                 len(cr["pages"]), sum(e["class"] == "HIRING" for e in emails), sum(e["class"] == "GENERAL" for e in emails),
                 f" - {cr['error']}" if cr.get("error") else "")
        if cr.get("error"):
            c.update(status="partial", notes=cr["error"])
            if "403" not in cr["error"] and "robots" not in cr["error"]:
                c["last_enriched_at"] = None  # transient: retry next run
        self._save_company(c)
        return c

    def enrich_job(self, job_id: str) -> dict:
        job = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            raise KeyError(job_id)
        job = dict(job)
        self.stats["jobs"] += 1
        # 1. on-demand posting details for board rows saved without a description
        if (self.e.get("fetch_board_details", True) and job.get("source") in ("linkedin", "zip_recruiter", "glassdoor")
                and len(job.get("description") or "") < 200 and (not job.get("enriched_at") or self.force)
                and not self.over_budget()):
            try:
                d = fetch_board_details(job)
                self.stats["details_fetched"] += 1
                if d:
                    sets = {k: v for k, v in d.items() if k in ("description", "company_industry", "company_url_direct",
                                                                "company_num_employees", "company_addresses", "job_level",
                                                                "job_function", "company_logo")}
                    if sets.get("description"):
                        from .sources.jobspy_source import extras

                        sets["emails"] = extras({"emails": [e for e, _ in find_emails(sets["description"])]})["emails"]
                    with self.conn:
                        self.conn.execute(f"UPDATE jobs SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
                                          (*sets.values(), job_id))
                    job.update(sets)
            except Exception as e:  # noqa: BLE001
                log.info("board details for %s failed: %s", job_id, e)
        # 2. the posting itself
        p_emails = posting_emails(job.get("description"), job.get("emails"), job.get("url"))
        rname = recruiter_name(job.get("description"))
        # 3. company
        siblings = [dict(r) for r in self.conn.execute(
            "SELECT * FROM jobs WHERE company=? AND id<>? ORDER BY fetched_at DESC LIMIT 20", (job.get("company"), job_id))]
        comp = self.enrich_company(job, siblings)
        # 4. direct apply link
        apply_url, apply_src = None, None
        if job.get("source") in ("greenhouse", "lever", "ashby"):
            apply_url, apply_src = job.get("url"), "company ATS posting"
        elif _good_direct(job.get("job_url_direct")):
            apply_url, apply_src = job["job_url_direct"], "employer apply link (via " + job["source"] + ")"
        elif job.get("source") == "google" and _good_direct(job.get("url")):
            apply_url, apply_src = job["url"], "employer apply link (via google)"
        if not apply_url and comp.get("ats_board_url") and not self.over_budget():
            b = ats_board_of(comp["ats_board_url"])
            if b:
                hit = match_on_ats(self.f, b[0], b[1], job.get("title") or "")
                if hit:
                    apply_url, apply_src = hit, f"same job on the company's {b[0]} board"
        if not apply_url:
            for u in re.findall(r"https?://[^\s)\]>\"'*]+", unescape_md(job.get("description"))):
                if ATS_HOSTS.search(host_of(u)) and "ziprecruiter" not in u:
                    apply_url, apply_src = u.rstrip(".,"), "ATS link in the posting"
                    break
        if not apply_url and comp.get("ats_board_url"):
            apply_url, apply_src = comp["ats_board_url"], "company's job board (ATS)"
        if not apply_url and comp.get("careers_url"):
            apply_url, apply_src = comp["careers_url"], "company careers page"
        if not apply_url:
            apply_url, apply_src = job.get("url"), f"{job.get('source')} posting (no direct link found)"
        with self.conn:
            self.conn.execute("UPDATE jobs SET company_key=?, apply_url=?, apply_url_source=?, recruiter_name=?, posting_emails=?,"
                              " enriched_at=? WHERE id=?",
                              (comp.get("company_key"), apply_url, apply_src, rname, json.dumps(p_emails), _now(), job_id))
        # 5. lead contact: only an EMPTY contact, only a HIRING address read verbatim
        if self.e.get("auto_set_contact", True):
            self._maybe_set_contact(job_id, rname)
        return brief(self.conn, job_id)

    def _maybe_set_contact(self, job_id: str, rname: str | None):
        from . import followups

        lead = self.conn.execute("SELECT * FROM leads WHERE job_id=?", (job_id,)).fetchone()
        if not lead or lead["status"] != "active" or (lead["contact_email"] or "").strip():
            return
        b = brief(self.conn, job_id)
        h = b.get("hiring_email")
        if not h:
            return
        name = lead["contact_name"] or (rname if h.get("source") == "posting" else None)
        followups.set_contact(self.conn, self.cfg, job_id, name, h["email"])
        with self.conn:  # mark it as found automatically, with the page it was read from
            self.conn.execute("UPDATE leads SET contact_source=? WHERE job_id=? AND contact_email=?",
                              (f"enrich: {h.get('source_url') or h.get('source')}", job_id, h["email"]))
        self.stats["contacts_set"].append({"job_id": job_id, "email": h["email"], "source_url": h.get("source_url")})
        log.info("lead %s: contact set to %s (from %s)", job_id[:10], h["email"], h.get("source_url"))


def brief(conn, job_id: str) -> dict:
    """Everything the digest/UI shows about a job's company, in one dict."""
    j = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not j:
        return {}
    j = dict(j)
    key = j.get("company_key") or company_key(j.get("company"))
    c = conn.execute("SELECT * FROM companies WHERE company_key=?", (key,)).fetchone()
    c = dict(c) if c else {}
    p_emails = _loads(j.get("posting_emails"))
    hiring = [e for e in p_emails if e["class"] == "HIRING"] + _loads(c.get("hiring_emails"))
    # role inbox on a careers page beats one in a footer; posting beats both
    hiring.sort(key=lambda e: (e.get("source") != "posting", not re.search(r"career|job|employ|join", e.get("source_url") or "", re.I)))
    general = [e for e in p_emails if e["class"] == "GENERAL"] + _loads(c.get("general_emails"))
    ignored = [e for e in p_emails if e["class"] == "IGNORE"] + _loads(c.get("ignored_emails"))
    lead = conn.execute("SELECT contact_name, contact_email, contact_source FROM leads WHERE job_id=?", (job_id,)).fetchone()
    return {
        "job_id": job_id, "company": j.get("company"), "company_key": key, "status": c.get("status") or ("" if j.get("enriched_at") else "not enriched"),
        "website": c.get("website") or (j.get("company_url_direct") if _good_direct(j.get("company_url_direct")) else None),
        "website_source": c.get("website_source"), "careers_url": c.get("careers_url"), "ats_board_url": c.get("ats_board_url"),
        "apply_url": j.get("apply_url") or (j.get("job_url_direct") if _good_direct(j.get("job_url_direct")) else None) or j.get("url"),
        "apply_url_source": j.get("apply_url_source") or "", "posting_url": j.get("url"),
        "hiring_email": hiring[0] if hiring else None, "hiring_emails": _merge_emails(hiring),
        "general_emails": _merge_emails(general), "ignored_emails": _merge_emails(ignored),
        "recruiter_name": j.get("recruiter_name"), "phone": c.get("phone"), "phone_source": c.get("phone_source"),
        "hq": re.sub(r"\s*\n\s*", "; ", (c.get("hq") or j.get("company_addresses") or "").strip()) or None, "description": c.get("description") or _one_line(j.get("company_description")),
        "industry": c.get("industry") or j.get("company_industry"), "size": c.get("size") or j.get("company_num_employees"),
        "revenue": j.get("company_revenue"), "linkedin_url": c.get("linkedin_url"),
        "board_company_url": j.get("company_url"), "notes": c.get("notes"), "last_enriched_at": c.get("last_enriched_at"),
        "enriched_at": j.get("enriched_at"), "pages_fetched": _loads(c.get("pages_fetched")),
        "lead_contact": dict(lead) if lead else None,
    }


def md_lines(b: dict, indent: str = "   ") -> list[str]:
    """Digest lines for one job's company info."""
    if not b:
        return []
    if b.get("status") == "anonymous":
        return [f"{indent}- company: anonymous listing (name withheld) - apply via {b.get('apply_url') or '-'}"]
    parts = []
    if b.get("website"):
        parts.append(f"website {b['website']}")
    if b.get("careers_url"):
        parts.append(f"careers {b['careers_url']}")
    if b.get("phone"):
        parts.append(f"phone {b['phone']}")
    if b.get("hq"):
        parts.append(f"HQ {b['hq']}")
    out = []
    if b.get("description"):
        out.append(f"{indent}- about: {b['description']}")
    out.append(f"{indent}- company: " + (" · ".join(parts) if parts else "no website found"))
    out.append(f"{indent}- apply: {b.get('apply_url') or '-'}" + (f" ({b['apply_url_source']})" if b.get("apply_url_source") else ""))
    h = b.get("hiring_email")
    if h:
        who = f" ({b['recruiter_name']})" if b.get("recruiter_name") and h.get("source") == "posting" else ""
        out.append(f"{indent}- hiring email: {h['email']}{who} - source: {h.get('source_url') or h.get('source')}")
    else:
        out.append(f"{indent}- no hiring email published - apply via {b.get('apply_url') or b.get('careers_url') or '-'}")
    if b.get("general_emails"):
        out.append(f"{indent}- GENERAL (not used for follow-ups): " + ", ".join(e["email"] for e in b["general_emails"][:2]))
    return out


def enrich_jobs(conn, cfg: dict, job_ids, budget_s: float | None = None, max_companies: int | None = None,
                force: bool = False) -> dict:
    """Enrich these jobs (company-level results are cached for enrich.cache_days). Never raises."""
    if not (cfg.get("enrich") or {}).get("enabled", True):
        return {"skipped": "enrich.enabled is false"}
    en = Enricher(conn, cfg, budget_s, max_companies, force)
    for jid in dict.fromkeys(job_ids):
        try:
            en.enrich_job(jid)
        except Exception as e:  # noqa: BLE001
            log.warning("enrich %s failed: %s", jid, e)
            en.stats["errors"].append(f"{jid[:10]}: {type(e).__name__}: {str(e)[:200]}")
    en.stats.update(seconds=round(time.time() - en.t0, 1), http_requests=en.f.requests, searches=en.search.calls)
    return en.stats


def targets(conn, cfg: dict, top: int = 0, leads: bool = True) -> list[str]:
    """Active leads (those still without a contact first), then the `top` highest-scored jobs."""
    ids: list[str] = []
    if leads:
        ids += [r[0] for r in conn.execute(
            "SELECT job_id FROM leads WHERE status='active' ORDER BY (COALESCE(contact_email,'')<>''), qualified_at DESC")]
    if top:
        a = cfg.get("auto") or {}
        excl = re.compile(a["exclude_title_regex"], re.I) if a.get("exclude_title_regex") else None
        n = 0
        for jid, title in conn.execute("SELECT id, title FROM jobs ORDER BY score DESC LIMIT ?", (top * 3,)):
            if excl and excl.search(title or ""):
                continue
            ids.append(jid)
            n += 1
            if n >= top:
                break
    return list(dict.fromkeys(ids))
