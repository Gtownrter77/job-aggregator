"""Normalization helpers: dedupe hash, metro matching, remote detection, text cleanup."""
from __future__ import annotations

import hashlib
import html
import math
import re
from datetime import datetime, timezone

from bs4 import BeautifulSoup

_COMPANY_SUFFIXES = re.compile(
    r"\b(incorporated|inc|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|plc|gmbh|lp|llp|the|group|holdings)\b\.?",
    re.I,
)
_LOC_NOISE = re.compile(r"\b(united states of america|united states|usa|us|u\.s\.a?\.?|hybrid|on-?site|in office)\b", re.I)
_STATE_FULL = {"georgia": "ga"}
_REMOTE_RE = re.compile(r"\b(remote|work from home|wfh|anywhere|distributed)\b", re.I)


def _clean(s: str | None) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_company(s: str | None) -> str:
    return _clean(_COMPANY_SUFFIXES.sub(" ", s or ""))


def norm_title(s: str | None) -> str:
    s = re.sub(r"\(.*?\)", " ", s or "")  # drop parentheticals like (Remote) / (Contract)
    return _clean(s)


def norm_location(s: str | None) -> str:
    s = _LOC_NOISE.sub(" ", s or "")
    s = _clean(s)
    for full, abbr in _STATE_FULL.items():
        s = re.sub(rf"\b{full}\b", abbr, s)
    # keep only the first location of a multi-location string, city + state
    return s


def dedupe_hash(company: str | None, title: str | None, location: str | None) -> str:
    key = f"{norm_company(company)}|{norm_title(title)}|{norm_location(location)}"
    return hashlib.sha1(key.encode()).hexdigest()


def job_id(source: str, source_job_id: str | None, url: str | None) -> str:
    return hashlib.sha1(f"{source}:{source_job_id or url}".encode()).hexdigest()


def is_remote_text(*texts: str | None) -> bool:
    return any(t and _REMOTE_RE.search(t) for t in texts)


def html_to_text(s: str | None) -> str:
    if not s:
        return ""
    s = html.unescape(s)  # Greenhouse double-escapes its HTML
    if "<" in s:
        s = BeautifulSoup(s, "html.parser").get_text("\n")
    s = re.sub(r"[ \t\xa0]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n\n", s).strip()


_SAL_RE = re.compile(
    r"\$\s?(\d{2,3}(?:,\d{3})+|\d{2,3}(?:\.\d+)?\s?[kK])\s*(?:-|–|—|to)\s*\$?\s?(\d{2,3}(?:,\d{3})+|\d{2,3}(?:\.\d+)?\s?[kK])"
)


def _money(v: str) -> float:
    v = v.replace(",", "").replace(" ", "")
    if v.lower().endswith("k"):
        return float(v[:-1]) * 1000
    return float(v)


def parse_salary_text(text: str | None) -> tuple[float | None, float | None, str | None]:
    """Best-effort USD annual range from free text, e.g. '$120,000 - $150,000'."""
    if not text:
        return None, None, None
    m = _SAL_RE.search(text)
    if not m:
        return None, None, None
    lo, hi = _money(m.group(1)), _money(m.group(2))
    if lo < 10000 or hi < lo:
        return None, None, None
    return lo, hi, "USD"


def num(v) -> float | None:
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def to_iso(v) -> str | None:
    """Accept datetime/date/str/epoch-ms and return ISO-8601 (UTC for datetimes)."""
    if v is None or v == "" or (isinstance(v, float) and math.isnan(v)):
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, tz=timezone.utc).isoformat(timespec="seconds")
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc).isoformat(timespec="seconds")
    if hasattr(v, "isoformat"):
        return v.isoformat()
    s = str(v).strip()
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return to_iso(d)
    except ValueError:
        return s or None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class MetroMatcher:
    """Decides whether a free-text location is inside the configured metro."""

    def __init__(self, metro_cfg: dict):
        towns = [t for t in metro_cfg.get("towns") or []]
        amb = [t for t in metro_cfg.get("towns_require_state") or []]
        states = metro_cfg.get("state_tokens") or []
        self.enabled = bool(towns or amb)
        self._towns = re.compile(r"\b(" + "|".join(re.escape(t) for t in towns) + r")\b", re.I) if towns else None
        # ambiguous town must be followed (within a few chars) by a state token
        if amb and states:
            self._amb = re.compile(
                r"\b(" + "|".join(re.escape(t) for t in amb) + r")\b[\s,\-]{0,4}(" + "|".join(re.escape(s) for s in states) + r")\b",
                re.I,
            )
            # also catch formats like "US-GA-Marietta" / "GA - Decatur"
            self._amb_rev = re.compile(
                r"\b(" + "|".join(re.escape(s) for s in states) + r")\b[\s,\-]{1,4}(" + "|".join(re.escape(t) for t in amb) + r")\b",
            )
        else:
            self._amb = self._amb_rev = None

    def matches(self, *locations: str | None) -> bool:
        if not self.enabled:
            return True
        for loc in locations:
            if not loc:
                continue
            if self._towns and self._towns.search(loc):
                return True
            if self._amb and (self._amb.search(loc) or self._amb_rev.search(loc)):
                return True
        return False


# ---- remote region ---------------------------------------------------------
# Where a remote posting says you may work from, when it says so. Order matters
# only for display; every matching region is kept ("US, Canada").
_US_STATES = ("Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|Hawaii|Idaho|"
              "Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|"
              "Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|"
              "North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|"
              "South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming|"
              "San Francisco|Seattle|Austin|Boston|Chicago|Denver|Atlanta|Los Angeles|NYC|Washington,? DC")
_REGIONS: list[tuple[str, re.Pattern]] = [
    ("Worldwide", re.compile(r"\b(worldwide|world-wide|global(ly)?|anywhere|any ?where in the world|all countries|international)\b", re.I)),
    ("US", re.compile(r"\b((?i:united states|usa)|US|AMER)\b|\bU\.S\.|" + rf"\b({_US_STATES})\b|,\s*(A[LKZR]|C[AOT]|D[EC]|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]|N[CDEHJMVY]|O[HKR]|PA|RI|S[CD]|T[NX]|UT|V[AT]|W[AIVY])\b")),
    ("Canada", re.compile(r"\b(canada|canadian|toronto|vancouver|montreal|ontario|quebec|british columbia|alberta)\b", re.I)),
    ("North America", re.compile(r"\b(north america|americas)\b", re.I)),
    ("LATAM", re.compile(r"\b(latam|latin america|south america|mexico|brazil|brasil|argentina|colombia|chile|peru|uruguay|costa rica|são paulo|sao paulo|buenos aires|bogot[aá]|mexico city)\b", re.I)),
    ("UK", re.compile(r"\b(UK|U\.K\.|united kingdom|england|scotland|wales|london|manchester|edinburgh)\b")),
    ("UK", re.compile(r"\b(united kingdom|england|scotland|london)\b", re.I)),
    ("Ireland", re.compile(r"\b(ireland|dublin)\b", re.I)),
    ("EMEA", re.compile(r"\bEMEA\b")),
    ("Europe", re.compile(r"\b(europe|european|EU|EEA|CET)\b|\b(germany|berlin|munich|france|paris|spain|madrid|barcelona|netherlands|amsterdam|"
                          r"portugal|lisbon|poland|warsaw|italy|sweden|stockholm|denmark|copenhagen|norway|finland|switzerland|zurich|"
                          r"austria|belgium|czech|prague|romania|greece|hungary|estonia|lithuania|latvia|croatia|serbia|ukraine|bulgaria)\b", re.I)),
    ("India", re.compile(r"\b(india|bangalore|bengaluru|hyderabad|pune|mumbai|delhi|chennai|gurgaon|gurugram|noida)\b", re.I)),
    ("APAC", re.compile(r"\b(APAC|asia|asia[- ]pacific|singapore|japan|tokyo|korea|seoul|philippines|manila|vietnam|indonesia|malaysia|thailand|hong kong|taiwan|china)\b", re.I)),
    ("Australia/NZ", re.compile(r"\b(australia|sydney|melbourne|new zealand|auckland|ANZ)\b", re.I)),
    ("Middle East/Africa", re.compile(r"\b(israel|tel aviv|UAE|dubai|saudi|egypt|nigeria|kenya|south africa|africa|middle east|MENA)\b", re.I)),
]
_ANYWHERE_DESC = re.compile(r"\b(work from anywhere|fully remote,? (from )?anywhere|remote[- ]first,? (team )?(across|around) the (world|globe)|"
                            r"hire (talent |people )?(from )?anywhere in the world|open to candidates (located )?anywhere)\b", re.I)


def remote_region(*location_texts: str | None, description: str | None = None) -> str:
    """'US', 'Worldwide', 'Europe', 'US, Canada', ... from the posting's location strings;
    falls back to explicit 'work from anywhere' wording in the description, else 'Unspecified'."""
    text = " | ".join(t for t in location_texts if t)
    # strip the word "remote" & friends so "Remote - US" -> " - US"
    text = re.sub(r"\b(remote|hybrid|work from home|wfh|distributed|virtual)\b", " ", text, flags=re.I)
    found: list[str] = []
    for name, rx in _REGIONS:
        if name not in found and rx.search(text):
            found.append(name)
    if not found and description and _ANYWHERE_DESC.search(description[:6000]):
        found.append("Worldwide")
    return ", ".join(found) if found else "Unspecified"
