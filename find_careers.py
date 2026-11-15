#!/usr/bin/env python3
"""
find_careers.py - locate careers / job-listing pages for a list of company websites.

For each site it:
  1. Loads the homepage and scores every link by its text, URL path, subdomain,
     and whether it points at a known applicant-tracking system (Workday,
     Greenhouse, Lever, iCIMS, ...).
  2. If nothing on the homepage looks like a careers link, probes common paths
     (/careers, /jobs, careers.<domain>, ...), then the sitemap.
  3. Fetches the top candidates, checks their content for job-posting signals,
     and picks the best one.
  4. Follows through from the careers landing page to the actual listings
     (ATS board, "Search jobs" link, embedded job widget).

Usage:
    python find_careers.py sites.txt -o results.csv
    python find_careers.py companies.csv --render      # use a headless browser for JS-heavy sites
"""
from __future__ import annotations

import argparse
import csv
import gzip
import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, fields
from functools import lru_cache
from urllib import robotparser
from urllib.parse import unquote, urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("career_sifter")

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# Identify honestly as a script rather than imitating a browser.
USER_AGENT = "CareerSifter/0.1 (job-market research script)"
ROBOTS_AGENT = "CareerSifter"

# Link text -> weight. Matched as whole words/phrases against anchor text,
# aria-label, title and image alt text.
LISTING_PHRASES = {
    "see openings": 10, "view openings": 10, "current openings": 10,
    "job openings": 10, "all openings": 10, "view all openings": 10,
    "open positions": 10, "open roles": 10, "explore roles": 9,
    "search jobs": 10, "search all jobs": 10, "view jobs": 10, "view all jobs": 10,
    "see jobs": 10, "see all jobs": 10, "browse jobs": 10, "find jobs": 10,
    "explore jobs": 10, "all jobs": 9, "job search": 9, "find your job": 9,
    "find your role": 9, "search careers": 9, "search opportunities": 9,
    "explore opportunities": 8, "job opportunities": 9, "career opportunities": 9,
    "current vacancies": 10, "vacancies": 8, "apply now": 5,
}
CAREER_PHRASES = {
    **LISTING_PHRASES,
    "careers": 8, "career": 6, "careers at": 9, "jobs": 7, "job": 4,
    "join us": 7, "join our team": 8, "join the team": 8,
    "work with us": 7, "work for us": 7, "work at": 5,
    "we're hiring": 8, "we are hiring": 8, "hiring": 4,
    "openings": 7, "opportunities": 3, "employment": 4,
    "life at": 4, "talent": 2,
}

# URL path segment -> weight.
PATH_TOKENS = {
    "careers": 8, "career": 7, "jobs": 8, "job": 5, "job-openings": 8,
    "open-positions": 8, "openings": 7, "vacancies": 7, "join-us": 7, "joinus": 7,
    "join-our-team": 7, "join": 4, "work-with-us": 7, "workwithus": 7,
    "work-for-us": 7, "employment": 4, "hiring": 4, "opportunities": 3,
    "talent": 2, "search-jobs": 9, "job-search": 9,
}
STUDENT_CAREER_RE = re.compile(r"career[- ]?(?:center|centre|services|development|education|exploration)|"
                               r"handshake|student[- ]?(?:jobs|employment|career)|alumni", re.I)
EDU_EMPLOYMENT_RE = re.compile(r"\b(?:employment|staff (?:jobs|positions)|faculty (?:jobs|positions)|"
                               r"work at|jobs at|working at)\b", re.I)
CAREER_SUBDOMAINS ={"careers", "career", "jobs", "join", "talent", "recruiting", "hiring"}

# Substrings in link text or path that suggest a non-careers page.
NEGATIVE_TOKENS = {
    "fraud": 6, "scam": 6, "privacy": 5, "cookie": 5, "terms": 4, "saved": 6,
    "investor": 5, "press": 4, "news": 4, "blog": 4,
    "story": 2, "stories": 2, "article": 2, "event": 2, "webinar": 3, "podcast": 3,
}

# Job boards / socials: never the company's own careers page.
EXCLUDED_HOSTS = (
    "linkedin.com", "glassdoor.", "indeed.", "facebook.com", "twitter.com",
    "x.com", "youtube.com", "instagram.com", "ziprecruiter.", "monster.",
)
SKIP_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".doc",
                   ".docx", ".zip", ".mp4", ".xls", ".xlsx", ".ppt", ".pptx")

# Applicant-tracking systems, matched against host+path or raw HTML.
ATS_PATTERNS = [
    ("Workday", r"myworkdayjobs\.com|myworkdaysite\.com|wd\d+\.myworkday"),
    ("Greenhouse", r"greenhouse\.io"),
    ("Lever", r"jobs\.lever\.co|api\.lever\.co"),
    ("iCIMS", r"icims\.com"),
    ("SmartRecruiters", r"smartrecruiters\.com"),
    ("Taleo", r"taleo\.net"),
    ("SuccessFactors", r"successfactors\.(?:com|eu)|sapsf\.(?:com|eu)|jobs\.sap\.com"),
    ("Oracle Cloud HCM", r"oraclecloud\.com/hcmui|hcmui/candidateexperience"),
    ("Attrax", r"attraxcdn"),
    ("PeopleAdmin", r"peopleadmin\.com|pa-hrsuite"),
    ("Jobvite", r"jobvite\.com"),
    ("Ashby", r"ashbyhq\.com"),
    ("BambooHR", r"bamboohr\.com/(?:careers|jobs)"),
    ("Workable", r"apply\.workable\.com|workable\.com/j"),
    ("Recruitee", r"recruitee\.com"),
    ("Teamtailor", r"teamtailor\.com"),
    ("Breezy", r"breezy\.hr"),
    ("JazzHR", r"applytojob\.com"),
    ("Personio", r"jobs\.personio\.(?:com|de)"),
    ("UKG", r"recruiting\d*\.ultipro\.com|rec\.pro\.ukg\.net"),
    ("ADP", r"workforcenow\.adp\.com|recruiting\.adp\.com"),
    ("Paylocity", r"recruiting\.paylocity\.com"),
    ("Dayforce", r"dayforcehcm\.com"),
    ("Avature", r"avature\.net"),
    ("Eightfold", r"eightfold\.ai"),
    ("Phenom", r"phenompeople\.com"),
    ("Rippling", r"ats\.rippling\.com"),
    ("Pinpoint", r"pinpointhq\.com"),
    ("Comeet", r"comeet\.(?:co|com)/jobs"),
]
# (?<![a-z0-9-]) stops "clever.co" matching "lever.co" etc.
_ATS_RE = [(name, re.compile(r"(?<![a-z0-9-])(?:" + pat + ")")) for name, pat in ATS_PATTERNS]

COMMON_PATHS = [
    "/careers", "/jobs", "/join-us", "/work-with-us", "/about/careers",
    "/about-us/careers", "/company/careers", "/en/careers", "/us/careers",
    "/careers.html", "/join",
]

# Workday data centres (wdN.myworkdayjobs.com), most common first, and usual external site names
WORKDAY_DATACENTERS = (1, 5, 3, 12, 2, 10, 103, 108, 501)
WORKDAY_SITE_GUESSES = ("Careers", "External", "{Slug}Careers", "{slug}", "{slug}careers", "{slug}_careers",
                        "{slug}-careers", "ExternalCareers", "External_Careers", "Careers_External",
                        "{SLUG}", "{Slug}", "{Slug}_Careers", "jobs", "careers")

MIN_LINK_SCORE = 5    # a homepage link must score at least this to be a candidate
MIN_LISTING_SCORE = 8  # a link on the careers page must score this to count as "the listings"
MIN_PAGE_SCORE = 6     # a probed/sitemap page must have this much content signal
JS_LINK_THRESHOLD = 15  # fewer <a> tags than this suggests a JS-rendered page

JOBPOSTING_RE = re.compile(r'"@type"\s*:\s*\[?\s*"JobPosting"', re.I)


# --------------------------------------------------------------------------- #
# Data types
# --------------------------------------------------------------------------- #

@dataclass
class Page:
    url: str
    status: int
    html: str = ""
    link_count: int = 0

    @property
    def ok(self) -> bool:
        return self.status == 200 and bool(self.html)


@dataclass
class Candidate:
    url: str
    score: int
    text: str = ""


@dataclass
class PageSignals:
    score: int = 0
    ats: str = ""
    ats_url: str = ""
    jobposting: bool = False
    reasons: list[str] = field(default_factory=list)


@dataclass
class Result:
    input_url: str
    company: str = ""
    status: str = "not_found"   # listings_found | careers_page_found | not_found | blocked | error
    rejected_requests: str = ""  # hosts that refused us, e.g. "www.x.com 403 Cloudflare"
    careers_url: str = ""
    listings_url: str = ""
    ats: str = ""
    score: int = 0
    method: str = ""
    final_url: str = ""
    top_candidates: str = ""
    notes: str = ""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def norm_text(s: str) -> str:
    s = s.lower().replace("’", "'").replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


@lru_cache(maxsize=None)
def phrase_re(phrase: str) -> re.Pattern:
    return re.compile(r"(?<![a-z])" + re.escape(phrase) + r"(?![a-z])")


def url_key(url: str) -> str:
    """Normalised key for de-duplicating URLs."""
    p = urlparse(urldefrag(url)[0])
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = p.path.rstrip("/") or "/"
    return f"{host}{path}" + (f"?{p.query}" if p.query else "")


def registered_domain(host: str | None) -> str:
    host = (host or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if (len(parts) >= 3 and len(parts[-1]) == 2
            and parts[-2] in {"co", "com", "ac", "org", "net", "gov", "edu", "ne", "or"}):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def detect_ats(text: str) -> str:
    text = text.lower()
    for name, rx in _ATS_RE:
        if rx.search(text):
            return name
    return ""


# Platforms whose career site lives on the company's own domain (only their CDN assets reveal them)
SAME_DOMAIN_ATS = {"Phenom", "Attrax"}
ASSET_EXTENSIONS =(".css", ".js", ".json", ".woff", ".woff2", ".ttf", ".ico", ".map") + SKIP_EXTENSIONS
JUNK_QUERY_PARAMS = re.compile(r"^(utm_.*|elementor.*|ver|gclid|fbclid|_ga|mc_.*)$", re.I)


def find_ats_url(html: str) -> tuple[str, str]:
    """Return (ats_name, ATS link) found anywhere in the HTML.

    An ATS seen only in script/stylesheet URLs (e.g. a Phenom CDN) gives a name but no
    link, meaning the jobs widget runs on the page itself.
    """
    asset_name = ""
    best: tuple[int, str, str] | None = None
    for m in re.finditer(r"""https?://[^\s"'<>\\)]+""", html):
        p = urlparse(m.group(0))
        name = detect_ats(p.netloc + p.path)
        if not name:
            continue
        # Phenom/Attrax serve the career site from the company's own domain; their hosts are only assets
        if (name in SAME_DOMAIN_ATS or p.path.lower().endswith(ASSET_EXTENSIONS)
                or p.netloc.startswith(("cdn.", "static.", "assets.", "www.icims."))):
            asset_name = asset_name or name
            continue
        # prefer public boards over employee-only portals and login pages
        penalty = 2 * p.netloc.lower().startswith("internal") + bool(re.search(r"login|signin", p.path, re.I))
        if best is None or penalty < best[0]:
            best = (penalty, name, m.group(0))
        if penalty == 0:
            break
    if best:
        return best[1], best[2]
    return asset_name, ""


def clean_url(url: str) -> str:
    """Drop tracking/preview query params and fragments."""
    p = urlparse(urldefrag(url)[0])
    if not p.query:
        return p.geturl()
    q = "&".join(kv for kv in p.query.split("&") if not JUNK_QUERY_PARAMS.match(kv.split("=")[0]))
    return p._replace(query=q).geturl()


def ats_board_url(url: str) -> str:
    """Turn a link to a single ATS job into the company's job-board root where the pattern is known."""
    url = clean_url(url)
    p = urlparse(url)
    host = p.netloc.lower()
    segs = [s for s in p.path.split("/") if s]
    if not segs:
        return url
    root = f"{p.scheme}://{p.netloc}/"
    if "smartrecruiters.com" in host and segs[:2] == ["web-sso", "saml"] and len(segs) > 2:
        return f"https://jobs.smartrecruiters.com/{segs[2]}"  # employee SSO link names the company
    if host in ("jobs.ashbyhq.com", "jobs.lever.co", "jobs.smartrecruiters.com",
                "careers.smartrecruiters.com", "boards.greenhouse.io", "job-boards.greenhouse.io") \
            and segs[0] != "embed":
        return root + segs[0]
    if host.endswith(".icims.com"):
        return f"{p.scheme}://{p.netloc}/jobs/search?ss=1"
    if (m := re.match(r"^/([a-z]{2}(?:-[a-z]{2})?)/job/[^/]*-jid-\d+", p.path, re.I)):
        return f"{root}{m.group(1)}/jobs"  # Attrax single-job link -> the site's job search
    if "myworkdayjobs.com" in host or "myworkdaysite.com" in host:
        # /en-US/SiteName/job/... -> /SiteName ; skip an optional locale segment
        site = segs[1] if re.fullmatch(r"[a-z]{2}(-[A-Za-z]{2})?", segs[0]) and len(segs) > 1 else segs[0]
        return root + site
    return url


def board_company_name(board_url: str) -> str:
    """Best guess at the company from a job-board URL: 'benchling' from jobs.ashbyhq.com/benchling,
    'pfizer' from pfizer.wd1.myworkdayjobs.com, 'werfen' from careers-werfen.icims.com."""
    p = urlparse(board_url)
    first_label = p.netloc.split(".")[0]
    segs = [s for s in p.path.split("/") if s]
    if p.netloc in ("jobs.ashbyhq.com", "jobs.lever.co", "jobs.eu.lever.co", "boards.greenhouse.io",
                    "job-boards.greenhouse.io", "jobs.smartrecruiters.com", "careers.smartrecruiters.com") and segs:
        return segs[0]
    return re.sub(r"^(?:[a-z]*careers|jobs|external|us)-", "", first_label)


def score_link(text: str, url: str, base_domain: str, phrases: dict[str, int]) -> int:
    p = urlparse(url)
    host = (p.hostname or "").lower()
    path = unquote(p.path).lower()
    if any(h in host for h in EXCLUDED_HOSTS) or path.endswith(SKIP_EXTENSIONS):
        return -100

    score = 0
    ats = detect_ats(host + path)
    if ats:
        score += 12
        if re.search(r"login|signin", path):
            score -= 10  # a login page, not the public board (still usable: iCIMS boards are normalised)
        if host.startswith("internal"):
            score -= 20  # employee-only portal

    t = norm_text(text)
    hits = [w for ph, w in phrases.items() if t and phrase_re(ph).search(t)]
    if hits:
        score += max(hits) + min(len(hits) - 1, 2)
    if len(t) > 60:  # long anchor text = probably an article teaser
        score -= 3

    segs = [s.split(".")[0] for s in path.split("/") if s]
    seg_hits = [PATH_TOKENS[s] for s in segs if s in PATH_TOKENS]
    seg_hits += [6 for s in segs if s.startswith("career") and s not in PATH_TOKENS]
    if seg_hits:
        score += max(seg_hits)

    if host.split(".")[0] in CAREER_SUBDOMAINS:
        score += 8

    score -= sum(w for tok, w in NEGATIVE_TOKENS.items() if tok in t or tok in path)

    # Universities: "Career Center" pages help students find jobs elsewhere; we want employment.
    if base_domain.endswith(".edu"):
        if STUDENT_CAREER_RE.search(t) or STUDENT_CAREER_RE.search(path) or host.split(".")[0] == "career":
            score -= 12
        if host.split(".")[0] in ("hr", "employment", "jobs", "careers") or EDU_EMPLOYMENT_RE.search(t):
            score += 3

    if not ats and registered_domain(host) != base_domain:
        score -= 3
    return score


def extract_candidates(html: str, page_url: str, base_domain: str,
                       phrases: dict[str, int] = CAREER_PHRASES) -> tuple[list[Candidate], int]:
    """Score all links on a page. Returns (candidates sorted best-first, total link count)."""
    soup = BeautifulSoup(html, "html.parser")
    best: dict[str, Candidate] = {}
    anchors = soup.find_all("a", href=True)
    for a in anchors:
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urldefrag(urljoin(page_url, href))[0]
        if not url.startswith(("http://", "https://")):
            continue
        text = " ".join(filter(None, [
            a.get_text(" ", strip=True), a.get("aria-label"), a.get("title"),
            *(img.get("alt") for img in a.find_all("img")),
        ]))
        s = score_link(text, url, base_domain, phrases)
        if s <= 0:
            continue
        k = url_key(url)
        if k not in best or s > best[k].score:
            best[k] = Candidate(url, s, text[:80])
    ranked = sorted(best.values(), key=lambda c: (-c.score, len(c.url)))
    return ranked, len(anchors)


def analyze_page(page: Page) -> PageSignals:
    """How much does this page look like a careers / job-listings page?"""
    sig = PageSignals()
    html_l = page.html.lower()
    soup = BeautifulSoup(page.html, "html.parser")
    title = norm_text(soup.title.get_text()) if soup.title else ""
    body = norm_text(soup.get_text(" "))

    if JOBPOSTING_RE.search(page.html):
        sig.jobposting = True
        sig.score += 12
        sig.reasons.append("JobPosting schema")

    sig.ats = detect_ats(urlparse(page.url).netloc + urlparse(page.url).path)
    if sig.ats:
        sig.ats_url = page.url
    else:
        sig.ats, sig.ats_url = find_ats_url(html_l)
    if sig.ats:
        sig.score += 10
        sig.reasons.append(f"ATS:{sig.ats}")

    if any(phrase_re(w).search(title) for w in ("careers", "career", "jobs", "job", "join us", "vacancies")):
        sig.score += 4
        sig.reasons.append("title")

    listing_hits = sum(1 for ph in LISTING_PHRASES if phrase_re(ph).search(body))
    if listing_hits:
        sig.score += min(listing_hits * 2, 6)
        sig.reasons.append(f"{listing_hits} listing phrases")

    if len(phrase_re("apply").findall(body)) >= 3:
        sig.score += 3
        sig.reasons.append("apply x3+")
    return sig


def load_sites(path: str) -> list[tuple[str, str]]:
    """Return [(url, company_name)]. Accepts .txt or .csv.

    .txt: one entry per line, either a URL or "Company Name, URL". URLs can be a homepage,
    a careers page, or the job board itself.
    """
    sites: list[tuple[str, str]] = []
    if path.lower().endswith(".csv"):
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            cols = {c.lower().strip(): c for c in reader.fieldnames or []}
            url_col = next((cols[c] for c in ("url", "website", "site", "domain", "homepage") if c in cols),
                           (reader.fieldnames or [None])[0])
            name_col = next((cols[c] for c in ("company", "name", "organization") if c in cols), None)
            for row in reader:
                url = (row.get(url_col) or "").strip()
                if url:
                    sites.append((url, (row.get(name_col) or "").strip() if name_col else ""))
    else:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                name, sep, url = line.rpartition(",")
                if sep and " " not in url.strip() and "." in url:
                    sites.append((url.strip(), name.strip()))
                else:
                    sites.append((line, ""))
    return sites


REJECT_STATUSES = {202, 401, 403, 405, 406, 429, 451, 503}
# (label, header test, body markers) for common bot-protection services
_BLOCKERS = [
    ("Cloudflare", lambda h: h.get("server", "") == "cloudflare" or "cf-ray" in h or "cf-mitigated" in h,
     ("cf-challenge", "just a moment...", "attention required! | cloudflare")),
    ("Akamai", lambda h: "akamai" in h.get("server", ""), ("access denied", "reference #")),
    ("Imperva", lambda h: "x-iinfo" in h or "x-cdn" in h and "imperva" in h["x-cdn"], ("incapsula",)),
    ("AWS WAF", lambda h: "x-amzn-waf-action" in h, ("aws waf",)),
    ("CloudFront", lambda h: h.get("server", "") == "cloudfront" or "x-amz-cf-id" in h, ("cloudfront",)),
    ("DataDome", lambda h: "x-datadome" in h, ("datadome",)),
    ("PerimeterX", lambda h: False, ("perimeterx", "px-captcha")),
]


def describe_rejection(r: requests.Response) -> str:
    """Return e.g. '403 Cloudflare' if the server refused the request, else ''.

    404s and plain outages are not rejections; a 503 only counts when a bot-protection
    service is behind it.
    """
    if r.status_code not in REJECT_STATUSES:
        return ""
    h = {k.lower(): v.lower() for k, v in r.headers.items()}
    try:
        body = r.text[:20000].lower()
    except Exception:
        body = ""
    vendor = next((name for name, hdr, marks in _BLOCKERS if hdr(h) or any(m in body for m in marks)), "")
    if not vendor and "captcha" in body:
        vendor = "CAPTCHA"
    if r.status_code == 503 and not vendor:
        return ""
    if r.status_code == 202:  # AWS WAF answers 202 with a JavaScript challenge instead of the page
        return "202 AWS WAF challenge" if h.get("x-amzn-waf-action") == "challenge" else ""
    return f"{r.status_code} {vendor}".strip()


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    # raise_on_status=False: after the last retry hand back the 429/503 response itself,
    # so callers can see (and record) the rejection instead of a generic RetryError
    retry = Retry(total=2, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=("GET", "HEAD", "POST"), raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


# --------------------------------------------------------------------------- #
# Per-site scanner
# --------------------------------------------------------------------------- #

class SiteScanner:
    def __init__(self, input_url: str, company: str, args: argparse.Namespace):
        self.args = args
        self.session = make_session()
        self.result = Result(input_url=input_url, company=company)
        self.notes: list[str] = []
        self.cache: dict[str, Page | None] = {}
        self.robots: dict[str, robotparser.RobotFileParser | None] = {}
        self.rejections: dict[str, str] = {}  # "host status vendor" -> first URL refused
        self.base_domain = ""
        self.home_key = ""

    def note_rejection(self, r: requests.Response) -> None:
        label = describe_rejection(r)
        if label:
            self.rejections.setdefault(f"{urlparse(r.url).netloc} {label}", r.url)

    # ---- networking ------------------------------------------------------- #

    def _robots_for(self, url: str) -> robotparser.RobotFileParser | None:
        p = urlparse(url)
        host = f"{p.scheme}://{p.netloc}"
        if host not in self.robots:
            rp = None
            try:
                # not recorded as a rejection: real blocks show up on the page request itself,
                # and guessed ATS hosts answer 401/403 here for boards that don't exist
                r = self.session.get(host + "/robots.txt", timeout=self.args.timeout)
                rp = robotparser.RobotFileParser()
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                elif r.status_code >= 500:  # RFC 9309: server error -> assume disallow all
                    rp.disallow_all = True
                else:                       # 4xx / missing -> allow all
                    rp.allow_all = True
            except requests.RequestException:
                rp = None
            self.robots[host] = rp
        return self.robots[host]

    def allowed(self, url: str) -> bool:
        if self.args.ignore_robots:
            return True
        rp = self._robots_for(url)
        return rp is None or rp.can_fetch(ROBOTS_AGENT, url)

    def get(self, url: str, record_rejection: bool = True) -> requests.Response | None:
        if not self.allowed(url):
            if record_rejection:  # stay quiet about guessed addresses
                self.notes.append(f"robots.txt disallows {url}")
            return None
        time.sleep(self.args.delay)
        try:
            r = self.session.get(url, timeout=self.args.timeout, allow_redirects=True)
        except requests.RequestException as e:
            log.debug("GET %s failed: %s", url, e)
            return None
        if record_rejection:
            self.note_rejection(r)
        return r

    def fetch(self, url: str) -> Page | None:
        # exact key (keeps scheme and www.) so a failed bare domain doesn't mask its www. variant
        k = urldefrag(url)[0].rstrip("/")
        if k in self.cache:
            return self.cache[k]
        r = self.get(url)
        page = None
        if r is not None:
            ctype = r.headers.get("Content-Type", "")
            html = ""
            if "html" in ctype or "xml" in ctype or not ctype:
                if not r.encoding or r.encoding.lower() == "iso-8859-1":
                    r.encoding = r.apparent_encoding
                html = r.text
            page = Page(url=r.url, status=r.status_code, html=html)
            page.link_count = html.lower().count("<a ")
            self.cache[urldefrag(r.url)[0].rstrip("/")] = page
        self.cache[k] = page
        return page

    def render(self, url: str) -> Page | None:
        """Load a page in headless Chromium so JS-built navigation shows up."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.notes.append("--render requested but playwright is not installed")
            self.args.render = False
            return None
        if not self.allowed(url):
            return None
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    pg = browser.new_page(user_agent=USER_AGENT)
                    resp = pg.goto(url, timeout=self.args.timeout * 2000, wait_until="domcontentloaded")
                    pg.wait_for_timeout(2500)
                    html = pg.content()
                    page = Page(url=pg.url, status=resp.status if resp else 200, html=html)
                    page.link_count = html.lower().count("<a ")
                    return page
                finally:
                    browser.close()
        except Exception as e:  # playwright raises many types
            self.notes.append(f"render failed: {type(e).__name__}")
            return None

    # ---- discovery steps -------------------------------------------------- #

    def load_home(self) -> Page | None:
        raw = self.result.input_url.strip()
        if not re.match(r"^https?://", raw, re.I):
            raw = "https://" + raw
        p = urlparse(raw)
        host = p.netloc
        attempts = [raw]
        if not host.startswith("www."):
            attempts.append(f"https://www.{host}{p.path}")
        attempts.append(f"http://{host}{p.path}")
        last = None
        for url in attempts:
            page = self.fetch(url)
            if page and page.ok:
                return page
            last = page
        if self.rejections:
            # homepage refused us: careers sites are often separate and unprotected
            bare = host[4:] if host.startswith("www.") else host
            for sub in ("careers", "jobs"):
                page = self.fetch(f"https://{sub}.{bare}/")
                if page and page.ok:
                    self.notes.append(f"homepage blocked - started from {sub}.{bare}")
                    return page
        if self.args.render:
            page = self.render(raw)
            if page and page.ok:
                return page
        self.notes.append(f"homepage unreachable (status {last.status if last else 'no response'})")
        return None

    def input_is_board(self) -> bool:
        """If the input URL is already a known job board (e.g. a Workday or Ashby link pasted in
        because the company homepage blocks us), use it directly instead of crawling it."""
        raw = self.result.input_url.strip()
        url = raw if re.match(r"^https?://", raw, re.I) else "https://" + raw
        p = urlparse(url)
        ats = detect_ats(p.netloc + p.path)
        if not ats or ats in SAME_DOMAIN_ATS:
            return False
        board = ats_board_url(url)
        page = self.fetch(board)  # only to record a refusal, if any; fetch_jobs uses the API anyway
        self.result.final_url = page.url if page else ""
        if not self.result.company:
            self.result.company = board_company_name(board)
        self.result.careers_url = self.result.listings_url = board
        self.result.ats = ats
        self.result.method = "input-is-board"
        self.result.status = "listings_found"
        return True

    def probe_paths(self, home: Page) -> list[Candidate]:
        p = urlparse(home.url)
        root = f"{p.scheme}://{p.netloc}"
        urls = [root + path for path in COMMON_PATHS]
        urls += [f"https://{sub}.{self.base_domain}/" for sub in ("careers", "jobs")]
        found = []
        for url in urls:
            page = self.fetch(url)
            if not page or not page.ok or url_key(page.url) == self.home_key:
                continue  # missing, or redirected back to the homepage
            sig = analyze_page(page)
            if sig.score >= MIN_PAGE_SCORE:
                found.append(Candidate(page.url, sig.score, "path probe"))
                break
        return found

    def from_sitemap(self, home: Page) -> list[Candidate]:
        p = urlparse(home.url)
        root = f"{p.scheme}://{p.netloc}"
        rp = None if self.args.ignore_robots else self.robots.get(root)
        sitemaps = list((rp.site_maps() if rp else None) or []) or [root + "/sitemap.xml",
                                                                   root + "/sitemap_index.xml"]
        locs: list[str] = []
        queue, seen = sitemaps[:3], set()
        while queue and len(seen) < 6:
            sm = queue.pop(0)
            if sm in seen:
                continue
            seen.add(sm)
            r = self.get(sm)
            if r is None or r.status_code != 200:
                continue
            data = r.content
            if sm.endswith(".gz"):
                try:
                    data = gzip.decompress(data)
                except OSError:
                    continue
            xml = data.decode("utf-8", "ignore")
            found = re.findall(r"<loc>\s*(.*?)\s*</loc>", xml, re.I)
            if "<sitemapindex" in xml.lower():
                # prioritise child sitemaps that look like page/career lists
                found.sort(key=lambda u: not re.search(r"career|job|page", u, re.I))
                queue.extend(found[:4])
            else:
                locs.extend(found)
        cands = []
        for url in locs:
            s = score_link("", url, self.base_domain, CAREER_PHRASES)
            if s >= MIN_LINK_SCORE:
                cands.append(Candidate(url, s, "sitemap"))
        # prefer shallow URLs (/careers over /careers/some-article)
        cands.sort(key=lambda c: (-c.score, c.url.count("/"), len(c.url)))
        return cands

    def verify(self, cands: list[Candidate]) -> tuple[Candidate, Page, PageSignals, int] | None:
        """Fetch top candidates, combine link score with content score, return the best."""
        best = None
        for c in cands[: self.args.verify]:
            page = self.fetch(c.url)
            if page is None or not page.ok:
                # ATS pages often block plain requests; trust a strong ATS link anyway
                if detect_ats(c.url):
                    sig = PageSignals(score=0, ats=detect_ats(c.url), ats_url=c.url)
                    page = Page(url=c.url, status=page.status if page else 0)
                    total = c.score
                else:
                    continue
            else:
                if self.args.render and page.link_count < JS_LINK_THRESHOLD:
                    rendered = self.render(page.url)
                    if rendered and rendered.ok:
                        page = rendered
                if url_key(page.url) == self.home_key:
                    continue
                sig = analyze_page(page)
                total = c.score + sig.score
            if best is None or total > best[3]:
                best = (c, page, sig, total)
        return best

    def find_listings(self, page: Page, sig: PageSignals) -> tuple[str, str]:
        """From a careers landing page, find where the actual openings are. Returns (url, ats)."""
        if detect_ats(page.url) or sig.jobposting:
            return page.url, sig.ats
        if not page.html:
            return "", sig.ats
        cands, _ = extract_candidates(page.html, page.url, self.base_domain, LISTING_PHRASES)
        here = url_key(page.url)
        cands = [c for c in cands if url_key(c.url) != here and c.score >= MIN_LISTING_SCORE]
        if cands:
            target = cands[0]
            ats = detect_ats(target.url) or sig.ats
            if not detect_ats(target.url):
                # one more hop: the "search jobs" page may itself hand off to an ATS
                nxt = self.fetch(target.url)
                if nxt and nxt.ok:
                    s2 = analyze_page(nxt)
                    ats = s2.ats or ats
                    if s2.ats_url and not s2.jobposting and detect_ats(s2.ats_url) and \
                            urlparse(s2.ats_url).path.strip("/"):
                        return s2.ats_url, ats
                    return nxt.url, ats
            return target.url, ats
        if sig.ats_url:
            return sig.ats_url, sig.ats
        if sig.ats:  # ATS script embedded but no link: jobs render on this page
            return page.url, sig.ats
        return "", ""

    # ---- main ------------------------------------------------------------- #

    def probe_ats_boards(self) -> tuple[str, str] | None:
        """Many careers pages load their job board from JavaScript, so no ATS link is visible.
        Try the usual board addresses built from the company's domain name."""
        slug = self.base_domain.split(".")[0]
        if not slug:
            return None
        workday = self.probe_workday(slug)
        if workday:
            return "Workday", workday
        probes = [
            ("iCIMS", f"https://careers-{slug}.icims.com/jobs/search?ss=1&in_iframe=1",
             f"https://careers-{slug}.icims.com/jobs/search?ss=1"),
            ("iCIMS", f"https://uscareers-{slug}.icims.com/jobs/search?ss=1&in_iframe=1",
             f"https://uscareers-{slug}.icims.com/jobs/search?ss=1"),
            ("Greenhouse", f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
             f"https://boards.greenhouse.io/{slug}"),
            ("Lever", f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=1", f"https://jobs.lever.co/{slug}"),
            ("Ashby", f"https://api.ashbyhq.com/posting-api/job-board/{slug}", f"https://jobs.ashbyhq.com/{slug}"),
            ("SmartRecruiters", f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1",
             f"https://jobs.smartrecruiters.com/{slug}"),
        ]
        for ats, probe_url, board_url in probes:
            # guessed addresses: a 401/403 usually just means "no such board", so don't record it
            r = self.get(probe_url, record_rejection=False)
            if r is None or r.status_code != 200:
                continue
            try:
                if ats == "iCIMS":
                    # a live portal lists job links, or hands off to the company's iCIMS career site
                    has_jobs = bool(re.search(r"/jobs/\d+/[^\"'?]*/job", r.text)) or \
                        bool(re.search(r"window\.top\.location\.href\s*=", r.text))
                else:
                    data = r.json()
                    has_jobs = bool(data.get("jobs") if isinstance(data, dict) and "jobs" in data
                                    else data.get("totalFound") if isinstance(data, dict) else data)
            except ValueError:
                continue
            if has_jobs:
                return ats, board_url
        return None

    def post_json(self, url: str, body: dict) -> requests.Response | None:
        if not self.allowed(url):
            return None
        time.sleep(self.args.delay)
        try:
            return self.session.post(url, json=body, timeout=self.args.timeout, allow_redirects=False,
                                     headers={"Accept": "application/json"})
        except requests.RequestException:
            return None

    def probe_workday(self, slug: str) -> str | None:
        """Find a Workday board for tenant <slug>. Workday answers 422 when the tenant isn't hosted
        on a data centre and 404 when it is but the site name is wrong, so we first locate the
        data centre cheaply, then try common site names there."""
        body = {"limit": 1, "offset": 0, "searchText": "", "appliedFacets": {}}
        for dc in WORKDAY_DATACENTERS:
            host = f"https://{slug}.wd{dc}.myworkdayjobs.com"
            r = self.post_json(f"{host}/wday/cxs/{slug}/careersifter-probe/jobs", body)
            if r is None or r.status_code != 404:
                continue
            for site in dict.fromkeys(s.format(slug=slug, Slug=slug.capitalize(), SLUG=slug.upper())
                                      for s in WORKDAY_SITE_GUESSES):
                r = self.post_json(f"{host}/wday/cxs/{slug}/{site}/jobs", body)
                try:
                    if r is not None and r.status_code == 200 and r.json().get("total"):
                        return f"{host}/{site}"
                except ValueError:
                    continue
            self.notes.append(f"Workday tenant {slug}.wd{dc} exists but its site name wasn't guessable - "
                              f"paste the board URL as input")
            return None
        return None

    def scan(self) -> Result:
        try:
            self._scan()
            if not self.result.ats and self.result.status != "error":
                found = self.probe_ats_boards()
                if found:
                    self.result.ats, self.result.listings_url = found
                    self.result.status = "listings_found"
                    self.result.method = (self.result.method + "+" if self.result.method else "") + "ats-probe"
                    self.notes.append("job board guessed from the domain name - verify it is this company")
        except Exception as e:  # never let one site kill the batch
            log.exception("unexpected error on %s", self.result.input_url)
            self.result.status = "error"
            self.notes.append(f"{type(e).__name__}: {e}")
        self.result.rejected_requests = "; ".join(self.rejections)
        self.result.notes = "; ".join(dict.fromkeys(self.notes))
        return self.result

    def _scan(self) -> None:
        res = self.result
        if self.input_is_board():
            return
        raw = res.input_url.strip()
        # set early so the ATS-board fallback still works when the homepage blocks us
        self.base_domain = registered_domain(urlparse(raw if "//" in raw else "https://" + raw).hostname)
        home = self.load_home()
        if home is None:
            res.status = "blocked" if self.rejections else "error"
            return
        res.final_url = home.url
        self.home_key = url_key(home.url)
        self.base_domain = registered_domain(urlparse(home.url).hostname)

        cands, n_links = extract_candidates(home.html, home.url, self.base_domain)
        cands = [c for c in cands if c.score >= MIN_LINK_SCORE]
        method = "homepage-link"

        if self.args.render and (n_links < JS_LINK_THRESHOLD or not cands):
            rendered = self.render(home.url)
            if rendered and rendered.ok:
                rc, _ = extract_candidates(rendered.html, rendered.url, self.base_domain)
                rc = [c for c in rc if c.score >= MIN_LINK_SCORE]
                if rc and (not cands or rc[0].score > cands[0].score):
                    cands, method = rc, "homepage-rendered"
        if not cands and n_links < JS_LINK_THRESHOLD and not self.args.render:
            self.notes.append(f"homepage has only {n_links} links - may be JS-rendered, try --render")

        if not cands:
            cands, method = self.probe_paths(home), "path-probe"
        if not cands:
            cands, method = self.from_sitemap(home), "sitemap"
        if not cands:
            res.status = "not_found"
            return

        res.top_candidates = " | ".join(f"{c.url} ({c.score})" for c in cands[:3])
        best = self.verify(cands)
        if best is None:
            res.status = "not_found"
            self.notes.append("candidates found but none could be fetched")
            return
        cand, page, sig, total = best
        res.careers_url = page.url
        res.score = total
        res.method = method
        if sig.reasons:
            self.notes.append("signals: " + ", ".join(sig.reasons))

        listings, res.ats = self.find_listings(page, sig)
        res.listings_url = ats_board_url(listings) if listings else ""
        res.careers_url = clean_url(res.careers_url)
        res.status = "listings_found" if (res.listings_url or res.ats) else "careers_page_found"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Find careers / job-listing pages for a list of websites.")
    ap.add_argument("input", help=".txt (one URL per line) or .csv (with a url/website column)")
    ap.add_argument("-o", "--output", default="careers_results.csv", help="output CSV path")
    ap.add_argument("--workers", type=int, default=8, help="sites scanned in parallel (default 8)")
    ap.add_argument("--timeout", type=float, default=15, help="per-request timeout in seconds")
    ap.add_argument("--delay", type=float, default=0.5, help="pause between requests to the same site")
    ap.add_argument("--verify", type=int, default=3, help="top candidates to fetch and check (default 3)")
    ap.add_argument("--render", action="store_true",
                    help="use headless Chromium (playwright) for JS-rendered sites")
    ap.add_argument("--ignore-robots", action="store_true", help="do not check robots.txt")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(message)s")
    logging.getLogger("urllib3").setLevel(logging.ERROR)

    sites = load_sites(args.input)
    if not sites:
        log.error("no sites found in %s", args.input)
        return 1
    log.info("Scanning %d sites with %d workers...\n", len(sites), args.workers)

    results: list[Result | None] = [None] * len(sites)
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(SiteScanner(url, name, args).scan): i
                   for i, (url, name) in enumerate(sites)}
        for fut in as_completed(futures):
            i = futures[fut]
            r = results[i] = fut.result()
            done += 1
            where = r.listings_url or r.careers_url or r.notes
            ats = f" [{r.ats}]" if r.ats else ""
            rejected = f"  (rejected: {r.rejected_requests})" if r.rejected_requests else ""
            log.info("[%d/%d] %-30s %-19s%s %s%s", done, len(sites),
                     r.input_url[:30], r.status, ats, where, rejected)

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[fl.name for fl in fields(Result)])
        w.writeheader()
        for r in results:
            w.writerow(vars(r))

    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    log.info("\nDone. %s", ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    n_rejected = sum(1 for r in results if r.rejected_requests)
    if n_rejected:
        log.info("%d site(s) rejected at least one request - see the rejected_requests column", n_rejected)
    log.info("Results written to %s", args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
