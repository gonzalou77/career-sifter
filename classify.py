"""
classify.py - rules for tagging job postings.

  parse_location()        is a location string in the US, and which state(s)?
  classify_education()    which degrees a description mentions, and the lowest one
  min_years_experience()  smallest "N+ years of experience" figure in a description
  classify_level()        early / mid / senior / executive, and what the call was based on

All rules are plain regexes kept near the top of each section so they are easy to tune.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# Location
# --------------------------------------------------------------------------- #

US_STATES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA",
    "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE", "Florida": "FL", "Georgia": "GA",
    "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA",
    "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
    "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH",
    "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY", "North Carolina": "NC",
    "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA",
    "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN",
    "Texas": "TX", "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY", "District of Columbia": "DC",
    "Puerto Rico": "PR",
}
STATE_ABBRS = set(US_STATES.values())
US_COUNTRY_NAMES = {"united states", "united states of america", "usa", "us", "u.s.", "u.s.a.", "u.s"}

_STATE_NAME_RE = re.compile(
    r"\b(" + "|".join(re.escape(s) for s in sorted(US_STATES, key=len, reverse=True)) + r")\b", re.I)
_DC_RE = re.compile(r"\bwashington,?\s*d\.?\s?c\.?(?![a-z])", re.I)
_US_MARKER_RE = re.compile(r"\bunited states\b|(?<![A-Za-z])(?:US|USA|U\.S\.A?\.?)(?![A-Za-z])")
_US_MARKER_CI_RE = re.compile(r"(?<![a-z])usa(?![a-z])", re.I)
_STATE_ABBR_RES = [
    re.compile(r",\s*([A-Z]{2})(?:\s+\d{5}(?:-\d{4})?)?(?![A-Za-z])"),  # "Boston, MA" / "Boston, MA 02110"
    re.compile(r"\b(?:US|USA)\s*[-,:]?\s*([A-Z]{2})(?![A-Za-z])"),        # "USA - NY - New York", "US-MA-Boston", "US MA Remote"
    re.compile(r"\b([A-Z]{2})\s+\d{5}\b"),                                 # "MA 02110"
]
_NON_US_RE = re.compile(
    r"\b(canada|mexico|brazil|argentina|chile|colombia|peru|united kingdom|england|scotland|wales|"
    r"ireland|france|germany|spain|italy|portugal|netherlands|belgium|switzerland|austria|sweden|"
    r"norway|denmark|finland|poland|czech|hungary|romania|greece|turkey|türkiye|israel|india|"
    r"china|japan|korea|singapore|australia|new zealand|taiwan|hong kong|philippines|malaysia|"
    r"thailand|vietnam|indonesia|south africa|egypt|saudi arabia|united arab emirates|emea|apac|"
    r"latam|europe)\b", re.I)
_NON_US_CODE_RE = re.compile(
    r"(?<![A-Za-z])(UK|CAN|MEX|BRA|ARG|CHL|COL|PER|GBR|IRL|FRA|DEU|ESP|ITA|PRT|NLD|BEL|CHE|AUT|SWE|"
    r"NOR|DNK|FIN|POL|CZE|HUN|ROU|GRC|TUR|ISR|IND|CHN|JPN|KOR|SGP|AUS|NZL|TWN|HKG|PHL|MYS|THA|VNM|"
    r"IDN|ZAF|EGY|SAU|ARE)(?![A-Za-z])")
_CA_PROVINCE_RE = re.compile(r",\s*(ON|QC|BC|AB|MB|NS|NB|NL|PE|SK)(?![A-Za-z])")
_REMOTE_RE = re.compile(r"\bremote\b|\bvirtual\b|home[- ]based|work from home|\btelecommute", re.I)
_CITY_RE = re.compile(r"([A-Z][A-Za-z.' ]+?),\s*(?:[A-Z]{2}(?![A-Za-z])|" + _STATE_NAME_RE.pattern[2:-2] + r")")
_DASH_FMT_RE = re.compile(r"^(?:US|USA|United States(?: of America)?)\s*-\s*([^-]+?)\s*-\s*(.+)$", re.I)


@dataclass
class LocationInfo:
    is_us: bool | None          # None = can't tell (e.g. "Remote", "3 Locations")
    states: list[str] = field(default_factory=list)
    city: str = ""
    remote: bool = False


def state_abbr(name: str) -> str:
    name = (name or "").strip()
    if name.upper() in STATE_ABBRS:
        return name.upper()
    for full, ab in US_STATES.items():
        if full.lower() == name.lower():
            return ab
    return ""


def is_us_country(country: str) -> bool:
    return (country or "").strip().lower() in US_COUNTRY_NAMES


def parse_location(text: str) -> LocationInfo:
    text = (text or "").strip()
    info = LocationInfo(is_us=None, remote=bool(_REMOTE_RE.search(text)))
    if not text:
        return info

    named: list[str] = []
    rest = text
    if _DC_RE.search(rest):
        named.append("DC")
        rest = _DC_RE.sub(" ", rest)
    named += [US_STATES[m.group(1).title()] if m.group(1).title() in US_STATES else state_abbr(m.group(1))
              for m in _STATE_NAME_RE.finditer(rest)]
    rest = _STATE_NAME_RE.sub(" ", rest)  # so "New Mexico" doesn't look like Mexico

    abbrs = [m.group(1) for rx in _STATE_ABBR_RES for m in rx.finditer(text) if m.group(1) in STATE_ABBRS]
    us_marker = bool(_US_MARKER_RE.search(text) or _US_MARKER_CI_RE.search(text))
    non_us = bool(_NON_US_RE.search(rest) or _NON_US_CODE_RE.search(text) or _CA_PROVINCE_RE.search(text))

    # abbreviations alone are weak evidence ("Bangalore, IN" is India, not Indiana)
    if non_us and not us_marker and not named:
        abbrs = []
    states = list(dict.fromkeys(named + abbrs))

    if us_marker or states:
        info.is_us = True
    elif non_us:
        info.is_us = False
    info.states = states if info.is_us else []

    m = _DASH_FMT_RE.match(text)
    if m:
        info.city = m.group(2).strip()
    elif (m := _CITY_RE.search(text)):
        info.city = m.group(1).split(" - ")[-1].strip()
    return info


_STATE_AFTER_CITY = r"\s*,\s*(" + "|".join(sorted(STATE_ABBRS)) + "|" + \
    "|".join(re.escape(s) for s in sorted(US_STATES, key=len, reverse=True)) + r")(?![A-Za-z])"


def _city_key(city: str) -> str:
    return re.sub(r"[^a-z ]", "", city.split(" - ")[0].lower()).strip()


def infer_missing_states(jobs) -> int:
    """Fill in the state for US jobs that only name a site ("TARRYTOWN", "Lincoln").

    1. Learn city -> state from the same company's other postings ("Tarrytown, NY").
    2. Otherwise look for "<city>, <state>" in the job's own description.
    Returns how many jobs were filled.
    """
    known: dict[str, str] = {}
    for j in jobs:
        if j.is_us and len(j.states) == 1 and j.city:
            known.setdefault(_city_key(j.city), j.states[0])
    filled = 0
    for j in jobs:
        if not j.is_us or j.states or not j.city or not _city_key(j.city):
            continue
        state = known.get(_city_key(j.city))
        if not state and j.description:
            city = re.escape(j.city.split(" - ")[0].strip())
            m = re.search(r"(?i:\b" + city + r")" + _STATE_AFTER_CITY, j.description)
            state = state_abbr(m.group(1)) if m else ""
        if state:
            j.states = [state]
            filled += 1
    return filled


# --------------------------------------------------------------------------- #
# Education
# --------------------------------------------------------------------------- #

EDU_ORDER = ["High school/GED", "Associate", "Bachelor's", "Master's", "PhD", "Professional doctorate"]
EDU_RANK = {"High school/GED": 1, "Associate": 2, "Bachelor's": 3, "Master's": 4,
            "PhD": 5, "Professional doctorate": 5}
EDU_NOT_STATED = "Not stated"

# Spelled-out forms: safe to match anywhere in the description (case-insensitive).
_EDU_WORDS = {
    "High school/GED": r"\bhigh school\b|\bGED\b|\bsecondary school diploma\b",
    "Associate": r"\bassociate'?s?\s+degree\b|\bassociate of (?:science|arts|applied)\b|\b(?:2|two)[- ]year degree\b",
    "Bachelor's": r"\bbachelor'?s?\b|\bbaccalaureate\b|\b(?:4|four)[- ]year (?:college |university )?degree\b"
                  r"|\bundergraduate degree\b|\bcollege degree\b",
    "Master's": r"\bmaster'?s\b(?!\s+(?:data|file|record|batch|schedul|plan))|\bmaster of\b|\bmasters? degree\b",
    "PhD": r"\bph\.?\s?d\b|\bdoctorate\b|\bdoctoral degree\b|\bdoctor of philosophy\b",
    "Professional doctorate": r"\bdoctor of (?:pharmacy|medicine|veterinary medicine|dental)\b"
                              r"|\bmedical degree\b|\bveterinary degree\b",
}
# Abbreviations (case-sensitive): only counted inside a sentence that talks about
# degrees/education, so "MS Office" or "Boston, MA" don't count as a Master's.
_EDU_ABBRS = {
    "Bachelor's": r"(?<![A-Za-z])(?:BS|BSc|B\.S\.?|B\.Sc\.?|BA|B\.A\.|BEng|B\.Eng\.?|BSN)(?![A-Za-z])",
    "Master's": r"(?<![A-Za-z])(?:MS|MSc|M\.S\.?|M\.Sc\.?|M\.A\.|MBA|MPH|MEng|M\.Eng\.?|MSN|MPharm)(?![A-Za-z])"
                r"(?!\s*(?:Office|Excel|Word|Teams|Project|PowerPoint|Outlook|Access|Visio|SQL|Dynamics|365))",
    "Professional doctorate": r"(?<![A-Za-z])(?:PharmD|Pharm\.?\s?D\.?|MD|M\.D\.|DVM|D\.V\.M\.|VMD|DDS|D\.D\.S\.|JD|J\.D\.)(?![A-Za-z])",
}
_EDU_WORDS_RE = {k: re.compile(v, re.I) for k, v in _EDU_WORDS.items()}
_EDU_ABBRS_RE = {k: re.compile(v) for k, v in _EDU_ABBRS.items()}
_DEGREE_CONTEXT_RE = re.compile(
    r"degree|education|diploma|qualif|bachelor|master|ph\.?\s?d|doctor|graduate|B\.?S\.?/|/M\.?S\b", re.I)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])|[\n;•]")


def _normalise(text: str) -> str:
    return (text or "").replace("’", "'").replace("\xa0", " ")


def classify_education(text: str) -> tuple[list[str], str]:
    """Return (degree levels mentioned in order of EDU_ORDER, lowest level mentioned or 'Not stated')."""
    text = _normalise(text)
    found = {lvl for lvl, rx in _EDU_WORDS_RE.items() if rx.search(text)}
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        if _DEGREE_CONTEXT_RE.search(sentence):
            found |= {lvl for lvl, rx in _EDU_ABBRS_RE.items() if rx.search(sentence)}
    mentioned = [lvl for lvl in EDU_ORDER if lvl in found]
    if not mentioned:
        return [], EDU_NOT_STATED
    return mentioned, min(mentioned, key=lambda lvl: EDU_RANK[lvl])


# --------------------------------------------------------------------------- #
# Experience
# --------------------------------------------------------------------------- #

_YEARS_RE = re.compile(
    r"(?:minimum of |at least )?(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*\d{1,2}\s*)?\+?\s*years?'?"
    r"\s+(?:of\s+)?(?:[\w/,&()-]+\s+){0,6}?experience", re.I)


def min_years_experience(text: str) -> int | None:
    years = [int(m.group(1)) for m in _YEARS_RE.finditer(_normalise(text))]
    years = [y for y in years if y <= 30]
    return min(years) if years else None


# --------------------------------------------------------------------------- #
# Career level
# --------------------------------------------------------------------------- #

LEVELS = ["early", "mid", "senior", "executive"]
LEVEL_UNSPECIFIED = "unspecified"

# Title rules, first match wins. Order matters: "Associate Director" must hit the
# director rule before "associate" can push it to early-career.
TITLE_RULES = [
    ("executive", r"\b(chief|president|vice[- ]president|s?vp|evp|head of|executive director|"
                  r"general manager|managing director|ceo|cfo|cto|coo|cso|cio|chro|"
                  r"dean|provost|chancellor|department chair)\b"),
    ("senior",    r"\b(?:senior |sr\.? |associate |assoc\.? |assistant )?director\b"),
    # academic ranks (universities)
    ("early",     r"\bassistant professor\b"),
    ("mid",       r"\bassociate professor\b"),
    ("senior",    r"\b(?:full )?professor\b"),
    ("mid",       r"\b(?:senior|sr\.?)\s+(?:research\s+)?associate\b"),
    ("early",     r"\b(intern|internship|co-?op|apprentice(?:ship)?|trainee|new grad(?:uate)?|graduate program|"
                  r"entry[- ]level|junior|jr\.?|post-?doc(?:toral)?|summer student)\b"),
    ("senior",    r"\b(senior|sr\.?|principal|staff|lead|distinguished|expert)\b"),
    ("numeral",   r"(?<![A-Za-z])(I|II|III|IV|V)(?![A-Za-z])|\b(?i:level|grade)\s*([1-5])\b|"
                  # "Scientist 2", "System Administrator 3 (Hybrid)": a level number after a word,
                  # at the end of the title or before a separator
                  r"(?<=[A-Za-z])\s+([1-5])(?=\s*(?:$|[(,\-–/|]))"),
    ("early",     r"\b(associate scientist|research associate|associate engineer|associate specialist|"
                  r"assistant|technician)\b"),
    ("mid",       r"\b(manager|supervisor)\b"),
]
_TITLE_RULES_RE = [(lvl, re.compile(rx) if lvl == "numeral" else re.compile(rx, re.I)) for lvl, rx in TITLE_RULES]
_NUMERAL_LEVEL = {"I": "early", "1": "early", "II": "mid", "2": "mid", "III": "mid", "3": "mid",
                  "IV": "senior", "4": "senior", "V": "senior", "5": "senior"}

# Labels some ATSs supply (SmartRecruiters experienceLevel, Phenom jobLevel, ...).
_LABEL_RULES = [
    ("executive", r"executive|\bvp\b|vice president|c-level|officer"),
    ("senior",    r"director|senior|principal|expert"),
    ("mid",       r"mid|experienced|professional|manager|intermediate"),
    ("early",     r"intern|entry|graduate|junior|student|trainee|early|associate|apprentice"),
]
_LABEL_RULES_RE = [(lvl, re.compile(rx, re.I)) for lvl, rx in _LABEL_RULES]

_EARLY_DESC_RE = re.compile(r"\b(entry[- ]level|early[- ]career|recent (?:college )?graduates?|new graduates?)\b", re.I)


def level_from_label(label: str) -> str:
    for lvl, rx in _LABEL_RULES_RE:
        if rx.search(label or ""):
            return lvl
    return ""


def level_from_title(title: str) -> str:
    title = _normalise(title)
    for lvl, rx in _TITLE_RULES_RE:
        m = rx.search(title)
        if not m:
            continue
        if lvl == "numeral":
            return _NUMERAL_LEVEL[next(g for g in m.groups() if g)]
        return lvl
    return ""


def level_from_years(years: int | None) -> str:
    if years is None:
        return ""
    if years <= 2:
        return "early"
    if years <= 6:
        return "mid"
    if years <= 12:
        return "senior"
    return "executive"


def classify_level(title: str, description: str = "", ats_label: str = "",
                   years: int | None = None) -> tuple[str, str]:
    """Return (level, source) where source is title | ats | experience | description | ''.

    The title wins over the ATS label: labels are often coarse ("Manager with direct
    reports" on an Executive Director role) while a title level is explicit.
    """
    if (lvl := level_from_title(title)):
        return lvl, "title"
    if (lvl := level_from_label(ats_label)):
        return lvl, "ats"
    if (lvl := level_from_years(years)):
        return lvl, "experience"
    if _EARLY_DESC_RE.search(description or ""):
        return "early", "description"
    return LEVEL_UNSPECIFIED, ""
