"""
job_details.py - pull structured details out of a job description.

  extract_salary()   pay range, currency and period ("$72,000 - $189,200", "$18.6 - $33.2" hourly)
  extract_details()  work arrangement, schedule, employment term, contract length, shifts, travel,
                     visa sponsorship, relocation, bonus, equity, certifications / licences

Everything is plain regexes near the top of each section, like classify.py. A value the
posting doesn't state is left empty rather than guessed.
"""
from __future__ import annotations

import re

HOURS_PER_YEAR = 2080  # 40 h x 52 weeks, to compare hourly and salaried pay

# --------------------------------------------------------------------------- #
# Salary
# --------------------------------------------------------------------------- #

_CUR = r"(US\$|USD\s?\$?|C\$|CAD\s?\$?|\$|€|EUR\s?|£|GBP\s?)"
# (?!\d|,\d): the number ends here; a comma *after* it ("$110,000, plus bonus") is punctuation
_AMOUNT = r"(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)\s?([kK])?(?!\d|,\d)"
_RANGE_RE = re.compile(_CUR + r"\s?" + _AMOUNT + r"\s*(?:-|–|—|to|and)\s*(?:" + _CUR + r"\s?)?" + _AMOUNT)
_SINGLE_RE = re.compile(_CUR + r"\s?" + _AMOUNT)
_BIG_UNIT_RE = re.compile(r"^\s*(?:million|billion|mm\b|bn\b|m\b|b\b)", re.I)  # "$2.5 billion" is not pay
# whole words only: "rate" must not match "corporate", "base" "database", "earn" "learn"
_PAY_CONTEXT_RE = re.compile(r"\b(?:salary|salaries|pay|pay range|compensation|wages?|base|hiring range|hourly|"
                             r"rate|earn|earnings|annual(?:ly)?|per hour|per year)\b", re.I)
# an amount that is a benefit or a one-off, not pay: "$25,000 in student loan repayment",
# "$10,000 sign-on bonus", "a relocation allowance of up to $15,000" ("plus bonus" after a salary is fine)
_NOT_PAY_NOUNS = (r"(?:tuition|student loans?|loans?|reimburse\w*|bonus(?:es)?|sign[- ]on|signing|stipends?|"
                  r"relocation|allowances?|referral|grants?|awards?|donations?|scholarships?|revenue|funding|"
                  r"investment|credits?)")
_NOT_PAY_AFTER_RE = re.compile(r"^\s*(?:(?:in|of|for|toward|towards|per employee in)\s+)?(?:(?!plus\b|and\b|with\b)"
                               r"\w+\s+){0,2}" + _NOT_PAY_NOUNS + r"\b", re.I)
_NOT_PAY_BEFORE_RE = re.compile(r"\b" + _NOT_PAY_NOUNS + r"\b[^.$\n]{0,20}$", re.I)
_PERIOD_RES = [
    ("hour", re.compile(r"^.{0,40}?(?:per|an|a|/)\s*(?:hour|hr)\b|^.{0,40}?\bhourly\b", re.I | re.S)),
    ("week", re.compile(r"^.{0,40}?(?:per|a|/)\s*week\b|^.{0,40}?\bweekly\b", re.I | re.S)),
    ("month", re.compile(r"^.{0,40}?(?:per|a|/)\s*month\b|^.{0,40}?\bmonthly\b", re.I | re.S)),
    ("year", re.compile(r"^.{0,40}?(?:per|a|/)\s*(?:year|yr|annum)\b|^.{0,40}?\b(?:annual(?:ly)?|yearly)\b",
                        re.I | re.S)),
]
_PER_YEAR = {"hour": HOURS_PER_YEAR, "week": 52, "month": 12, "year": 1}
_PLAUSIBLE = {"hour": (7, 500), "week": (300, 20_000), "month": (1_000, 200_000), "year": (15_000, 2_000_000)}


def _amount(num: str, k: str | None) -> float:
    value = float(num.replace(",", ""))
    return value * 1000 if k else value


def _plausible(period: str, low: float, high: float) -> bool:
    lo, hi = _PLAUSIBLE.get(period, (0, 0))
    return bool(period) and lo <= low and high <= hi


def _period(after: str, low: float, high: float) -> str:
    """Period named right after the amount, else inferred from its size."""
    for name, rx in _PERIOD_RES:
        if rx.search(after) and _plausible(name, low, high):
            return name
    if high < 300:  # "$18.6 - $33.2" with no unit: hourly
        return "hour"
    if low >= 10_000:
        return "year"
    return ""


def _currency(symbol: str) -> str:
    s = symbol.replace(" ", "").upper()
    if s.startswith("C$") or s.startswith("CAD"):
        return "CAD"
    if s.startswith("€") or s.startswith("EUR"):
        return "EUR"
    if s.startswith("£") or s.startswith("GBP"):
        return "GBP"
    return "USD"


def salary_fields(low: float, high: float, currency: str, period: str, source: str) -> dict:
    mid = (low + high) / 2
    return {"salary_min": low, "salary_max": high, "salary_currency": currency, "salary_period": period,
            "salary_annual_mid": round(mid * _PER_YEAR[period]) if period in _PER_YEAR else None,
            "salary_source": source}


def extract_salary(text: str) -> dict:
    """Return salary_min/max/currency/period/annual_mid/source, or {} when no pay is stated.

    Ranges near pay words come first; a bare single amount counts only next to a pay word.
    Amounts outside a plausible band for their period (company revenue, bonuses) are ignored.
    """
    text = (text or "").replace("\xa0", " ")
    candidates = []
    for rx, is_range in ((_RANGE_RE, True), (_SINGLE_RE, False)):
        for m in rx.finditer(text):
            if _BIG_UNIT_RE.match(text[m.end():m.end() + 12]):
                continue
            g = m.groups()  # range: cur, num, k, cur2, num2, k2 / single: cur, num, k
            low = _amount(g[1], g[2])
            high = _amount(g[4], g[5]) if is_range else low
            if is_range and g[2] and not g[5]:  # "$150K - 200": same unit on both
                high *= 1000
            elif is_range and g[5] and not g[2]:  # "$120 - $150K", "$60-80k": the K covers both
                low *= 1000
            if high < low:
                low, high = high, low
            before = text[max(0, m.start() - 200):m.start()]
            after = text[m.end():m.end() + 60]
            if _NOT_PAY_AFTER_RE.match(after) or _NOT_PAY_BEFORE_RE.search(before):
                continue  # a benefit / bonus / revenue figure
            context = bool(_PAY_CONTEXT_RE.search(before) or _PAY_CONTEXT_RE.search(after[:40]))
            if not is_range and not context:
                continue
            period = _period(after, low, high)
            if not _plausible(period, low, high):  # company revenue, a bonus, a phone number...
                continue
            # best = in pay context, then a range, then earliest in the text
            candidates.append(((not context, not is_range, m.start()), low, high, _currency(g[0]), period))
    if not candidates:
        return {}
    _, low, high, cur, period = min(candidates)
    return salary_fields(low, high, cur, period, "description")


# --------------------------------------------------------------------------- #
# Other details
# --------------------------------------------------------------------------- #

_REMOTE_RE = re.compile(r"\b(?:fully remote|100% remote|remote[- ]first|remote[- ]based|remote (?:position|role|"
                        r"opportunity|work|employee)|work (?:from home|remotely)|(?:this|the) (?:position|role|job) "
                        r"is (?:fully )?remote)\b", re.I)
# not the science / IT senses: "hybrid capture" (sequencing), "hybrid seed" (agriculture), "hybrid cloud"
_HYBRID_RE = re.compile(r"\bhybrid\b(?![- ](?:capture|cloud|seeds?|corn|maize|rice|wheat|sorghum|crops?|plants?|"
                        r"vigou?r|breeding|varieties|variety|vehicles?|cells?|molecules?|proteins?|assays?|"
                        r"materials?|IT\b|infrastructure|architecture|mass spec|imaging|technology|methods?)\b)",
                        re.I)
_ONSITE_RE = re.compile(r"\b(?:100% on-?site|fully on-?site|on-?site (?:position|role|presence|work|requirement|"
                        r"is required)|(?:requires?|required to be) (?:\w+ ){0,3}on-?site|site-based|office-based|"
                        r"in-office (?:position|role))\b", re.I)

_INTERN_RE = re.compile(r"\b(?:intern(?:ship)?|co-?op|summer student)\b", re.I)
_FIXED_RE = re.compile(r"\b(?:fixed[- ](?:term|duration)|limited[- ]term|temporary (?:position|role|assignment|"
                       r"employee|appointment)|term[- ]limited)\b", re.I)
_CONTRACT_RE = re.compile(r"\b(?:contract(?:or)? (?:position|role|assignment|opportunity)|contract[- ]to[- ]hire|"
                          r"\b1099\b|W-?2 contract|this is a contract)\b", re.I)
# "contract" as in a contract job, not "contract manufacturing" / "contract research organisation"
_CONTRACT_WORD = (r"\bcontract\b(?!\s+(?:manufactur\w*|research|development|testing|lab\w*|organi[sz]ations?|"
                  r"partners?|vendors?|sites?|negotiat\w*|management|review|terms?|services?|CDMO|CMO|CRO)\b)")
_DURATION_RE = re.compile(
    r"(?:\bfixed[- ](?:term|duration)|" + _CONTRACT_WORD + r"|\btemporary|\bassignment|\bduration|"
    r"\blimited[- ]term|\bappointment)"
    r"[^.\n]{0,60}?\b(\d{1,2})[- ]?months?\b|\b(\d{1,2})[- ]?months?\b[^.\n]{0,40}?"
    r"(?:" + _CONTRACT_WORD + r"|\bassignment\b|(?<![-\w])term\b|\bduration\b|\bappointment\b|\bfixed\b)", re.I)
# "at least 18 months", "within 12 months", "6 months of experience": not a contract length
_NOT_DURATION_BEFORE_RE = re.compile(r"(?:at least|minimum(?: of)?|min\.?|within|after|first|every|past|over|"
                                     r"last|next|least)\s*$", re.I)
_NOT_DURATION_AFTER_RE = re.compile(r"^[- ]?months?\s+(?:of\s+)?(?:\w+\s+){0,2}experience", re.I)


def contract_months(text: str) -> int | None:
    """Length of a fixed-term / contract role in months, if the posting states one."""
    for m in _DURATION_RE.finditer(text):
        g = 1 if m.group(1) else 2
        if _NOT_DURATION_BEFORE_RE.search(text[max(0, m.start(g) - 15):m.start(g)]) or \
                _NOT_DURATION_AFTER_RE.match(text[m.end(g):m.end(g) + 50]):
            continue
        months = int(m.group(g))
        if 1 <= months <= 48:
            return months
    return None
_SHIFT_RE = re.compile(r"\b(1st|2nd|3rd|first|second|third|day|night|evening|overnight|weekend|rotating|swing|"
                       r"graveyard|(?:8|10|12)[- ]hour)\s+shifts?\b", re.I)
_SHIFT_NORM = {"first": "1st", "second": "2nd", "third": "3rd", "graveyard": "overnight"}
_TRAVEL_PCT_RE = re.compile(r"travel[^.%\n]{0,60}?(?:up to\s*)?(\d{1,3})\s*%|(?:up to\s*)?(\d{1,3})\s*%\s*"
                            r"(?:of\s*(?:the\s*)?time\s*)?(?:\w+\s+){0,2}travel", re.I)
_NO_TRAVEL_RE = re.compile(r"\bno travel\b|travel:\s*none|\b0\s*%\s*travel", re.I)
_TRAVEL_REQUIRED_RE = re.compile(r"travel (?:is )?required|requires? (?:\w+ ){0,3}travel|willing(?:ness)? to travel",
                                 re.I)
_NO_SPONSOR_RE = re.compile(
    r"(?:not|unable to|will not|won't|cannot|can't|does not|do not)\s+(?:\w+\s+){0,5}sponsor|"
    r"sponsorship (?:is |will )?not (?:be )?(?:available|offered|provided)|"
    r"without (?:the need for )?(?:current or future |now or in the future )?(?:\w+ )?sponsorship|"
    r"no (?:visa |immigration )?sponsorship|not eligible for (?:visa |immigration )?sponsorship", re.I)
_SPONSOR_RE = re.compile(r"(?:visa |immigration )?sponsorship (?:is |may be |will be )?(?:available|offered|"
                         r"provided)|will sponsor|able to sponsor|offers? (?:visa )?sponsorship", re.I)
_RELOCATION_RE = re.compile(r"relocation (?:assistance|package|support|benefits?|reimbursement)(?: (?:is|may be|will "
                            r"be|is also))?(?: (?:available|provided|offered))|(?:offer|provide)s?\s+relocation", re.I)
_NO_RELOCATION_RE = re.compile(r"(?:no|not eligible for|without|does not include|not offer(?:ing)?)\s+relocation|"
                               r"relocation (?:assistance )?(?:is |will )?not (?:be )?(?:available|provided|offered)",
                               re.I)
_BONUS_RE = re.compile(r"\bbonus(?:es)?\b", re.I)
_EQUITY_RE = re.compile(r"\b(?:equity|stock options?|RSUs?|restricted stock(?: units?)?|stock awards?|"
                        r"stock[- ]based (?:compensation|awards?)|long[- ]term incentives?|LTIs?)\b", re.I)
# "equity" that isn't compensation, removed before _EQUITY_RE looks: "diversity, equity and inclusion",
# "health equity", "pay equity", "private equity", "DE&I"
_NOT_EQUITY_RE = re.compile(
    r"\b(?:diversity|inclusion|inclusive|belonging|health|healthcare|pay|racial|social|gender|educational|"
    r"digital|access|accessibility|brand|private|home|shareholders?'?|stockholders?'?|treatment|internal|external|"
    r"excellence|courage|joy|integrity|respect|fairness)"
    r"[\s,&/-]+(?:and\s+|&\s*)?equity\b|\bequity[\s,&/-]+(?:and\s+|&\s*)?(?:inclusion|diversity|belonging|access|"
    r"justice|research|investors?|firms?|partners?|funds?|joy|excellence|accounting|analysis)\b|"
    r"\bequity[- ]minded\b", re.I)

# Certifications / licences: name -> regex. Acronyms are case-sensitive; (?i:...) marks
# the parts matched in any case.
CERTIFICATIONS = {
    "PMP": r"\bPMP\b",
    "Six Sigma": r"(?i:\bsix sigma\b|\b(?:green|black) belt\b)",
    "ASQ (CQE/CQA/CQM)": r"\b(?:CQE|CQA|CQM|CQT|CSQE|ASQ)\b",
    "Clinical research (CCRP/CCRC/ACRP/SoCRA)": r"\b(?:CCRP|CCRC|CCRA|ACRP|SoCRA)\b",
    "Regulatory (RAC)": r"\bRAC\b|(?i:regulatory affairs certification)",
    "Lab (ASCP/MLS)": r"\bASCP\b|\bMLS\b|(?i:medical laboratory scientist)",
    "Nursing (RN)": r"\bRN\b|(?i:\bregistered nurse\b)",
    "Pharmacist licence": r"(?i:licensed pharmacist|pharmacist licen[cs]e)|\bRPh\b",
    "Vet tech (RVT/LVT/CVT)": r"\b(?:RVT|LVT|CVT|LVMT)\b|(?i:(?:registered|licensed|certified) veterinary technician)",
    "Veterinary licence": r"(?i:licen[cs]ed? to practice veterinary|veterinary licen[cs]e|USDA accreditation)",
    # not "licensed physicians": Pfizer's standard Sunshine Act paragraph mentions them on every posting
    "Medical licence / board certified": r"(?i:board[- ]certifi|(?:active|current|valid|unrestricted|state) "
                                         r"(?:\w+ )?medical licen[cs]e|licensed to practi[cs]e medicine)",
    "Lab animal (AALAS)": r"\b(?:AALAS|ALAT|LATG)\b",
    "Crop adviser / pesticide applicator": r"(?i:certified crop advis|pesticide applicator)",
    "CPA": r"\bCPA\b",
    "Professional engineer (PE)": r"(?i:professional engineer(?:ing)? licen)|\bP\.E\. licen",
    "Cloud (AWS/Azure/GCP)": r"\bAWS Certified\b|(?i:\b(?:azure|google cloud) certifi)",
    "Security (CISSP)": r"\bCISSP\b",
    "HR (PHR/SHRM)": r"\b(?:PHR|SPHR|SHRM-CP|SHRM-SCP)\b",
    "CDL": r"\bCDL\b|(?i:commercial driver'?s licen)",
}
_CERT_RES = {name: re.compile(rx) for name, rx in CERTIFICATIONS.items()}

DETAIL_FIELDS = ["work_arrangement", "schedule", "employment_term", "contract_months", "shifts", "travel",
                 "visa_sponsorship", "relocation", "bonus", "equity", "certifications"]


def normalise_arrangement(raw: str) -> str:
    """Map a job board's own workplace label ("OnSite", "ORA_HYBRID", "Site-Dedicated") to remote/hybrid/on-site."""
    key = re.sub(r"[_\-\s]+", " ", (raw or "").strip().lower())
    if "hybrid" in key or "flexible" in key:
        return "hybrid"
    if "remote" in key:
        return "remote"
    if "site" in key or "office" in key:  # "OnSite", "ORA_ON_SITE", "Site-Dedicated", "In office"
        return "on-site"
    return ""


def extract_details(text: str, title: str = "", employment_type: str = "", arrangement: str = "") -> dict:
    """Pull the non-salary details. `employment_type` and `arrangement` are the job board's own
    fields when it has them; they win over what the description text suggests."""
    text = (text or "").replace("\xa0", " ").replace("’", "'")
    et = (employment_type or "").lower()
    d: dict = {k: "" for k in DETAIL_FIELDS}

    d["work_arrangement"] = normalise_arrangement(arrangement) or (
        "hybrid" if _HYBRID_RE.search(text) else "remote" if _REMOTE_RE.search(text)
        else "on-site" if _ONSITE_RE.search(text) else "")

    if "part" in et:
        d["schedule"] = "part-time"
    elif "full" in et:
        d["schedule"] = "full-time"
    elif re.search(r"\bpart[- ]time\b", text, re.I):
        d["schedule"] = "part-time"
    elif re.search(r"\bfull[- ]time\b", text, re.I):
        d["schedule"] = "full-time"

    if _INTERN_RE.search(title) or "intern" in et or "trainee" in et:
        d["employment_term"] = "internship"
    elif "fixed" in et or "temporary" in et or "temp" == et.strip() or _FIXED_RE.search(title + "\n" + text):
        d["employment_term"] = "fixed-term"
    elif "contract" in et or _CONTRACT_RE.search(title + "\n" + text):
        d["employment_term"] = "contract"
    elif "regular" in et or "permanent" in et:
        d["employment_term"] = "permanent"
    if d["employment_term"] in ("fixed-term", "contract", "internship", "") and \
            (months := contract_months(title + "\n" + text)):
        d["contract_months"] = months
        d["employment_term"] = d["employment_term"] or "fixed-term"

    shifts = []
    for m in _SHIFT_RE.finditer(title + "\n" + text):
        s = m.group(1).lower().replace(" ", "-")
        shifts.append(_SHIFT_NORM.get(s, s))
    d["shifts"] = "; ".join(dict.fromkeys(shifts))

    pcts = [int(a or b) for a, b in _TRAVEL_PCT_RE.findall(text) if 0 <= int(a or b) <= 100]
    if pcts:
        d["travel"] = f"up to {max(pcts)}%"
    elif _NO_TRAVEL_RE.search(text):
        d["travel"] = "none"
    elif _TRAVEL_REQUIRED_RE.search(text):
        d["travel"] = "required"

    if _NO_SPONSOR_RE.search(text):
        d["visa_sponsorship"] = "not offered"
    elif _SPONSOR_RE.search(text):
        d["visa_sponsorship"] = "offered"
    if _NO_RELOCATION_RE.search(text):
        d["relocation"] = "not offered"
    elif (m := _RELOCATION_RE.search(text)):
        # "may be available" (often standard wording on every posting) is not a firm offer
        d["relocation"] = "possible" if re.search(r"\bmay\b", m.group(0), re.I) else "offered"
    d["bonus"] = "yes" if _BONUS_RE.search(text) else ""
    d["equity"] = "yes" if _EQUITY_RE.search(_NOT_EQUITY_RE.sub(" ", text)) else ""
    d["certifications"] = [name for name, rx in _CERT_RES.items() if rx.search(text)]
    return d
