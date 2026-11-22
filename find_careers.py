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
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
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
    "talent": 2, "search-jobs": 9, "job-search": 9, "search-results": 6,
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

# Job-search pages of common career-site platforms (Phenom, Attrax, Radancy, SuccessFactors, ...),
# tried when a careers page itself won't load
CAREERS_SEARCH_PATHS = [
    "/us/en/search-results", "/global/en/search-results", "/en/search-results", "/us/en", "/global/en",
    "/en/jobs", "/en-us/jobs", "/jobs/search", "/search-jobs", "/en/search-jobs", "/jobs", "/search/?q=",
    "/careers/search", "/job-search",
]

COMMON_PATHS = [
    "/careers", "/jobs", "/join-us", "/work-with-us", "/about/careers",
    "/about-us/careers", "/company/careers", "/en/careers", "/us/careers",
    "/careers.html", "/join",
]

# Workday data centres (wdN.myworkdayjobs.com), most common first, and usual external site names
WORKDAY_DATACENTERS = (1, 5, 3, 12, 2, 10, 103, 108, 115, 501)
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
    input_type: str = ""        # homepage | careers_page | job_board: which entry path the scan took
    source: str = ""            # known (saved URL reused) | discovered | rediscovered (saved URL failed)
    status: str = "not_found"   # listings_found | careers_page_found | not_found | blocked | error
    rejected_requests: str = ""  # hosts that refused us, e.g. "www.x.com 403 Cloudflare"
    careers_url: str = ""
    listings_url: str = ""
    ats: str = ""
    score: int = 0
    method: str = ""
    final_url: str = ""
    top_candidates: str = ""
    manual_check: str = ""      # what to open by hand when no usable job board was found
    requests: int = 0           # HTTP requests this company cost
    notes: str = ""


PASTE_HINT = "paste it into careers_url in the input (or listings_url in known_sites.csv)"


def robots_note(url: str) -> str:
    """manual_check for a page or job board whose robots.txt disallows us: skipped, not collected."""
    host = urlparse(url if "//" in url else "https://" + url).netloc or url
    return (f"skipped: robots.txt on {host} asks scripts not to read {url} - open the company's careers search "
            f"page in a browser and try again with that careers search URL; {PASTE_HINT}")


def careers_failed_note(url: str) -> str:
    """manual_check for a careers page (given in the input or saved) that could not be loaded.
    The homepage is deliberately not tried instead: the user gave this page for a reason."""
    return (f"the careers page {url} could not be loaded - please manually check for the careers page URL or "
            f"job search URL and try again; {PASTE_HINT}")


def manual_check_note(r: Result) -> str:
    """Tell the user which page to open by hand, and where to put what they find."""
    paste = "paste the job listings URL into listings_url in known_sites.csv (or careers_url in the input)"
    if r.status == "listings_found" and r.ats:
        return ""
    if r.status == "listings_found":
        return f"no supported job board detected - open {r.listings_url} in a browser; {paste}"
    if r.status == "careers_page_found":
        return f"no job board detected - open {r.careers_url} in a browser, find the openings and {paste}"
    if r.status == "blocked":
        return f"site refuses scripts - open {r.input_url} in a browser, go to its careers page and {paste}"
    if r.status == "not_found":
        name = r.company or r.input_url
        return f"no careers page found - search '{name} careers' in a browser and {paste}"
    return f"could not load the site - check {r.input_url} in a browser and {paste}"


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
    if (m := re.match(r"^/([a-z]{2}(?:-[a-z]{2})?)/(?:job/[^/]*-jid-\d+|jobs(?:/|$))", p.path, re.I)):
        return f"{root}{m.group(1)}/jobs"  # Attrax job link or filtered search -> the site's job search
    if "myworkdayjobs.com" in host or "myworkdaysite.com" in host:
        # /en-US/SiteName/job/... -> /SiteName ; skip an optional locale segment
        site = segs[1] if re.fullmatch(r"[a-z]{2}(-[A-Za-z]{2})?", segs[0]) and len(segs) > 1 else segs[0]
        return root + site
    return url


def is_excluded_host(host: str) -> bool:
    """Social / job-aggregator site? Compared on the whole registered domain, so "x.com" (X/Twitter)
    doesn't also exclude careers.idexx.com or spacex.com."""
    rd = registered_domain(host)
    return any(rd == h or (h.endswith(".") and rd.startswith(h)) for h in EXCLUDED_HOSTS)


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
    if is_excluded_host(host) or path.endswith(SKIP_EXTENSIONS):
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
    if segs and re.fullmatch(r"[a-z]{2}(?:[-_][a-z]{2})?", segs[0]) and segs[0][:2] not in ("en", "us"):
        score -= 2  # same page in another language (/ko/careers, /de-de/karriere): prefer English
    seg_hits =[PATH_TOKENS[s] for s in segs if s in PATH_TOKENS]
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


def classify_input(url: str) -> str:
    """Decide where a scan should start: 'job_board', 'careers_page' or 'homepage'.

    careers_page: a careers subdomain (careers.x.com, jobs.x.edu) or a careers-like path
    (/careers, /jobs, /join-us, ...), or a company-domain career site platform (Phenom, Attrax).
    """
    p = urlparse(url if "//" in url else "https://" + url)
    ats = detect_ats((p.netloc + p.path).lower())
    if ats:
        return "careers_page" if ats in SAME_DOMAIN_ATS else "job_board"
    if (p.hostname or "").split(".")[0].lower() in CAREER_SUBDOMAINS | {"employment"}:
        return "careers_page"
    segs = [s.lower().split(".")[0] for s in p.path.split("/") if s]
    if any(PATH_TOKENS.get(s, 0) >= 4 or s.startswith("career") for s in segs):
        return "careers_page"
    return "homepage"


def load_sites(path: str) -> list[tuple[str, str, str]]:
    """Return [(url, company_name, careers_url)]. Accepts .txt or .csv.

    .txt: one entry per line, either a URL or "Company Name, URL". The URL can be a homepage,
    a careers page or the job board itself; the type is detected from the URL.
    .csv: a url/website column, optional company column, and an optional careers_url column.
    A filled careers_url makes that row start from the careers page and skip the homepage.
    """
    sites: list[tuple[str, str, str]] = []
    if path.lower().endswith(".csv"):
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            cols = {c.lower().strip(): c for c in reader.fieldnames or []}
            url_col = next((cols[c] for c in ("url", "website", "site", "domain", "homepage") if c in cols),
                           (reader.fieldnames or [None])[0])
            name_col = next((cols[c] for c in ("company", "name", "organization") if c in cols), None)
            careers_col = next((cols[c] for c in ("careers_url", "careers", "careers_page", "jobs_url") if c in cols),
                               None)
            for row in reader:
                url = (row.get(url_col) or "").strip()
                careers = (row.get(careers_col) or "").strip() if careers_col else ""
                if url or careers:
                    sites.append((url or careers, (row.get(name_col) or "").strip() if name_col else "", careers))
    else:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                name, sep, url = line.rpartition(",")
                if sep and " " not in url.strip() and "." in url:
                    sites.append((url.strip(), name.strip(), ""))
                else:
                    sites.append((line, "", ""))
    return sites


KNOWN_FIELDS = ["key", "company", "input_url", "status", "careers_url", "listings_url", "ats", "method",
                "last_verified", "last_checked", "manual_check"]
GOOD_STATUSES = ("listings_found", "careers_page_found")


def needs_manual(entry: dict) -> bool:
    """A saved entry with no usable job board: the user should look at it by hand."""
    status = entry.get("status") or ("listings_found" if entry.get("listings_url") else "careers_page_found")
    return not entry.get("listings_url") and status != "listings_found"


def checked_recently(entry: dict, days: int) -> bool:
    try:
        return (date.today() - date.fromisoformat(entry.get("last_checked", ""))).days < days
    except ValueError:
        return False


def site_key(url: str) -> str:
    """Key a company by its registered domain, so pfizer.com, www.pfizer.com and
    careers.pfizer.com share one saved entry. A pasted job board is keyed by the board."""
    full = url if "//" in url else "https://" + url
    if classify_input(full) == "job_board":
        return "board:" + url_key(ats_board_url(full))
    return registered_domain(urlparse(full).hostname)


def load_known(path: str) -> dict[str, dict]:
    """Saved careers pages / job boards from earlier runs, keyed by site_key()."""
    if not path or not os.path.exists(path):
        return {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        return {row["key"]: row for row in csv.DictReader(f) if row.get("key")}


def save_known(path: str, known: dict[str, dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=KNOWN_FIELDS, extrasaction="ignore")
        w.writeheader()
        for key in sorted(known):
            w.writerow(known[key])


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

# robots.txt shared across scanners for one run: host -> (parser, 5xx status or 0)
_ROBOTS_CACHE: dict[str, tuple[robotparser.RobotFileParser | None, int]] = {}
_ROBOTS_LOCKS: dict[str, threading.Lock] = {}
_ROBOTS_LOCKS_GUARD = threading.Lock()


def _robots_lock(host: str) -> threading.Lock:
    """One lock per host, so two scanners don't fetch the same robots.txt at once."""
    with _ROBOTS_LOCKS_GUARD:
        return _ROBOTS_LOCKS.setdefault(host, threading.Lock())


class SiteScanner:
    def __init__(self, input_url: str, company: str, args: argparse.Namespace, careers_url: str = "",
                 known: dict | None = None):
        self.args = args
        self.session = make_session()
        self.result = Result(input_url=input_url, company=company)
        self.careers_url = careers_url  # given in the input: start here, skip the homepage
        self.known = known              # saved entry from known_sites.csv, if any
        self.notes: list[str] = []
        self.cache: dict[str, Page | None] = {}
        self.robots: dict[str, robotparser.RobotFileParser | None] = {}
        self.robots_errors: dict[str, int] = {}  # host -> 5xx status its robots.txt returned
        self.rejections: dict[str, str] = {}  # "host status vendor" -> first URL refused
        self.requests = 0                     # HTTP requests this company cost
        self.robots_skipped = ""              # first page robots.txt kept us from (not guessed addresses)
        self.careers_failed = ""              # careers page that wouldn't load, nor its search pages
        self.base_domain = ""
        self.home_key = ""

    def note_rejection(self, r: requests.Response) -> None:
        label = describe_rejection(r)
        if label:
            self.rejections.setdefault(f"{urlparse(r.url).netloc} {label}", r.url)

    # ---- networking ------------------------------------------------------- #

    def _robots_for(self, url: str) -> robotparser.RobotFileParser | None:
        """robots.txt for url's host, fetched once per run and shared by all scanners
        (many companies probe the same hosts: api.lever.co, boards-api.greenhouse.io, ...)."""
        p = urlparse(url)
        host = f"{p.scheme}://{p.netloc}"
        if host not in self.robots:
            with _robots_lock(host):
                if host not in _ROBOTS_CACHE:
                    _ROBOTS_CACHE[host] = self._fetch_robots(host)
            self.robots[host], err = _ROBOTS_CACHE[host]
            if err:
                self.robots_errors[host] = err
        return self.robots[host]

    def _fetch_robots(self, host: str) -> tuple[robotparser.RobotFileParser | None, int]:
        rp, err = None, 0
        try:
            # not recorded as a rejection: real blocks show up on the page request itself,
            # and guessed ATS hosts answer 401/403 here for boards that don't exist
            self.requests += 1
            r = self.session.get(host + "/robots.txt", timeout=self.args.timeout)
            rp = robotparser.RobotFileParser()
            if r.status_code == 200:
                rp.parse(r.text.splitlines())
            elif r.status_code >= 500:  # RFC 9309: server error -> assume disallow all
                rp.disallow_all = True
                err = r.status_code
            else:                       # 4xx / missing -> allow all
                rp.allow_all = True
        except requests.RequestException:
            rp = None
        return rp, err

    def allowed(self, url: str) -> bool:
        if self.args.ignore_robots:
            return True
        rp = self._robots_for(url)
        return rp is None or rp.can_fetch(ROBOTS_AGENT, url)

    def get(self, url: str, record_rejection: bool = True) -> requests.Response | None:
        if not self.allowed(url):
            if record_rejection:  # stay quiet about guessed addresses
                p = urlparse(url)
                self.robots_skipped = self.robots_skipped or url
                err = self.robots_errors.get(f"{p.scheme}://{p.netloc}")
                self.notes.append(f"robots.txt on {p.netloc} unavailable (server error {err}) - treated as "
                                  f"disallowed, retry later" if err else f"robots.txt disallows {url}")
            return None
        time.sleep(self.args.delay)
        self.requests += 1
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
                    self.requests += 1  # page load only; the browser's own asset requests aren't counted
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
        if not p.path.strip("/"):
            # homepage unusable (refused, robots.txt, down): careers sites are often separate hosts
            bare = host[4:] if host.startswith("www.") else host
            for sub in ("careers", "jobs"):
                page = self.fetch(f"https://{sub}.{bare}/")
                if page and page.ok:
                    self.notes.append(f"homepage unusable - started from {sub}.{bare}")
                    return page
                if page is not None:  # the host exists but its root failed: try its search pages
                    page = self.try_search_pages(f"https://{sub}.{bare}")
                    if page:
                        self.notes.append(f"homepage unusable - started from search page {page.url}")
                        return page
        if self.args.render:
            page = self.render(raw)
            if page and page.ok:
                return page
        self.notes.append(f"homepage unreachable (status {last.status if last else 'no response'})")
        return None

    def use_board(self, url: str) -> None:
        """The input is already a known job board (e.g. a Workday or Ashby link pasted in because
        the company homepage blocks us): use it directly instead of crawling it."""
        ats = detect_ats(urlparse(url).netloc + urlparse(url).path)
        board = ats_board_url(url)
        page = self.fetch(board)  # only to record a refusal, if any; fetch_jobs uses the API anyway
        self.result.final_url = page.url if page else ""
        if page is not None and page.status in (404, 410):  # board no longer exists
            self.result.status = "not_found"
            self.notes.append(f"job board {board} answered {page.status}")
            return
        if not self.result.company:
            self.result.company = board_company_name(board)
        self.result.careers_url = self.result.listings_url = board
        self.result.ats = ats
        self.result.method = "input-is-board"
        self.result.status = "listings_found"

    def try_search_pages(self, origin: str, skip: str = "") -> Page | None:
        """Workaround for a careers page that won't load (e.g. careers.lilly.com refuses its root
        but serves /us/en): try the job-search pages career-site platforms usually have.
        Gives up after a few refusals in a row, since the whole host is then probably blocked."""
        refused = 0
        for path in CAREERS_SEARCH_PATHS:
            url = origin + path
            if url_key(url) == url_key(skip):
                continue
            page = self.fetch(url)
            if page is not None and page.ok and url_key(page.url) != url_key(f"https://{self.base_domain}/"):
                sig = analyze_page(page)
                if sig.ats or sig.score >= MIN_PAGE_SCORE:
                    return page
            refused = refused + 1 if page is not None and page.status in (401, 403, 405, 406, 429, 451) else 0
            if refused >= 3:
                self.notes.append(f"{urlparse(origin).netloc} refused 3 search pages in a row - stopped trying")
                return None
        return None

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
        fallback = ""
        if cands:
            target = cands[0]
            ats = detect_ats(target.url) or sig.ats
            if detect_ats(target.url):
                return target.url, ats
            # one more hop: the "search jobs" page may itself hand off to an ATS
            nxt = self.fetch(target.url)
            if nxt and nxt.ok:
                s2 = analyze_page(nxt)
                ats = s2.ats or ats
                if s2.ats_url and not s2.jobposting and detect_ats(s2.ats_url) and \
                        urlparse(s2.ats_url).path.strip("/"):
                    return s2.ats_url, ats
                return nxt.url, ats
            # the listings link was refused or broken (e.g. a blocked site root): don't report it
            # as the job board if this page itself already shows where the jobs are
            fallback = target.url
        if sig.ats_url:
            return sig.ats_url, sig.ats
        if sig.ats:  # ATS script embedded but no link: jobs render on this page
            return page.url, sig.ats
        return fallback, ""

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
        self.requests += 1
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
            if not self.result.ats:
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
        self.result.requests = self.requests
        self.result.manual_check = manual_check_note(self.result)
        if self.result.status in ("blocked", "error"):
            # the site couldn't be loaded: say why, and what to paste in. robots.txt first, since
            # retrying the same page won't help (not_found keeps its own "search for it" note)
            if self.robots_skipped:
                self.result.manual_check = robots_note(self.robots_skipped)
            elif self.careers_failed:
                self.result.manual_check = careers_failed_note(self.careers_failed)
        return self.result

    def _scan(self) -> None:
        """Two entry paths (plus pasted job boards):
        homepage     -> find the careers link -> careers page -> listings
        careers_page -> start at the careers page itself; the homepage is never requested,
                        so a homepage that blocks scripts can't stop the scan
        """
        res = self.result
        if self.known and not self.careers_url:
            self.scan_from_known()
            return
        raw = (self.careers_url or res.input_url).strip()
        url = raw if re.match(r"^https?://", raw, re.I) else "https://" + raw
        res.input_type = classify_input(url)
        if self.careers_url and res.input_type == "homepage":
            res.input_type = "careers_page"  # given explicitly in the careers_url column
        # set early so the ATS-board fallback still works when the first page blocks us
        self.base_domain = registered_domain(urlparse(url).hostname)

        if res.input_type == "job_board":
            self.use_board(url)
        elif res.input_type == "careers_page":
            self.scan_from_careers_page(url)
        else:
            self.scan_from_homepage()

    def scan_from_known(self) -> None:
        """Start from the job board / careers page saved by an earlier run (known_sites.csv),
        so the homepage is not needed. main() rediscovers from the homepage if this fails."""
        res, k = self.result, self.known
        res.source = "known"
        res.company = res.company or k.get("company", "")
        board, careers = k.get("listings_url", ""), k.get("careers_url", "")
        start = board or careers
        start = start if re.match(r"^https?://", start, re.I) else "https://" + start
        self.base_domain = registered_domain(urlparse(start).hostname)
        if board and classify_input(board) == "job_board":
            res.input_type = "job_board"
            self.use_board(board)
            if res.status == "listings_found":
                res.careers_url = careers or res.careers_url
                res.method = f"known ({k.get('method') or 'saved'})"
        else:
            res.input_type = "careers_page"
            self.scan_from_careers_page(start)
            if res.method:
                res.method = f"known+{res.method}"

    def scan_from_careers_page(self, url: str) -> None:
        res = self.result
        page = self.fetch(url)
        if (page is None or not page.ok) and self.args.render:
            page = self.render(url) or page
        if page is None or not page.ok:
            status = page.status if page else "no response"
            p = urlparse(url)
            page = self.try_search_pages(f"{p.scheme}://{p.netloc}", skip=url)
            if page is None:
                res.status = "blocked" if self.rejections else "error"
                self.careers_failed = url
                self.notes.append(f"careers page unreachable (status {status}), and none of its usual "
                                  f"search pages loaded - the homepage is not tried instead")
                return  # scan() still tries the usual job-board addresses for the domain
            self.notes.append(f"careers page unreachable (status {status}) - used its search page {page.url}")
        res.final_url = page.url
        homepage = url_key(f"https://{self.base_domain}/")
        if url_key(page.url) == homepage:
            self.notes.append("careers URL redirected to the homepage - searching it for a careers link")
            self.scan_from_homepage(page)
            return
        self.home_key = homepage
        if self.args.render and page.link_count < JS_LINK_THRESHOLD:
            rendered = self.render(page.url)
            if rendered and rendered.ok:
                page = rendered
        sig = analyze_page(page)
        self.finish(page, sig, sig.score, "careers-input")

    def scan_from_homepage(self, home: Page | None = None) -> None:
        res = self.result
        home = home or self.load_home()
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
        self.finish(page, sig, total, method)

    def finish(self, page: Page, sig: PageSignals, score: int, method: str) -> None:
        """Record the chosen careers page and follow it through to the listings."""
        res = self.result
        res.careers_url = page.url
        res.score = score
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
    ap.add_argument("--known", default="known_sites.csv",
                    help="saved careers pages / job boards reused on later runs (default known_sites.csv)")
    ap.add_argument("--no-known", action="store_true", help="don't read or update the saved-sites file")
    ap.add_argument("--refresh", action="store_true",
                    help="ignore saved entries and rediscover every site from its homepage")
    ap.add_argument("--recheck-days", type=int, default=30,
                    help="skip saved sites that need a manual check until they are this many days old "
                         "(default 30); paste a job board into known_sites.csv to use it sooner")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(message)s")
    logging.getLogger("urllib3").setLevel(logging.ERROR)

    sites = load_sites(args.input)
    if not sites:
        log.error("no sites found in %s", args.input)
        return 1
    if args.no_known:
        args.known = ""
    known = load_known(args.known)
    keys = [site_key(careers or url) for url, _, careers in sites]
    n_reuse = sum(1 for k, (_, _, careers) in zip(keys, sites)
                  if k in known and not careers and not args.refresh)
    log.info("Scanning %d sites with %d workers (%d from saved entries in %s)...\n",
             len(sites), args.workers, n_reuse, args.known or "-")

    def run(url: str, name: str, careers: str, key: str) -> Result:
        # an explicit careers_url in the input wins over a saved entry
        entry = None if (careers or args.refresh) else known.get(key)
        if entry and needs_manual(entry):
            if checked_recently(entry, args.recheck_days):
                # nothing new to find yet: don't repeat the same search, point at the manual step
                r = Result(input_url=url, company=name or entry.get("company", ""), source="known",
                           status=entry.get("status") or "careers_page_found",
                           careers_url=entry.get("careers_url", ""))
                r.manual_check = entry.get("manual_check") or manual_check_note(r)
                r.notes = (f"skipped: checked {entry['last_checked']} with no job board found; rechecked after "
                           f"{args.recheck_days} days or with --refresh")
                return r
            entry = None  # due for a full recheck from the homepage
        r = SiteScanner(url, name, args, careers, known=entry).scan()
        if entry is None:
            r.source = "discovered"
            return r
        if r.status in GOOD_STATUSES:
            return r
        # the saved URL stopped working (site redesign, moved board): rediscover from the input
        first = r
        r = SiteScanner(url, name, args, careers).scan()
        r.source = "rediscovered"
        r.notes = "; ".join(filter(None, [f"saved URL failed ({first.status}: {first.notes[:150]})", r.notes]))
        r.rejected_requests = "; ".join(filter(None, [first.rejected_requests, r.rejected_requests]))
        return r

    results: list[Result | None] = [None] * len(sites)
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run, url, name, careers, key): i
                   for i, ((url, name, careers), key) in enumerate(zip(sites, keys))}
        for fut in as_completed(futures):
            i = futures[fut]
            r = results[i] = fut.result()
            done += 1
            where = r.listings_url or r.careers_url or r.notes
            ats = f" [{r.ats}]" if r.ats else ""
            rejected = f"  (rejected: {r.rejected_requests})" if r.rejected_requests else ""
            log.info("[%d/%d] %-30s %-12s %-12s %-19s %3d req%s %s%s", done, len(sites),
                     r.input_url[:30], r.source, r.input_type, r.status, r.requests, ats, where, rejected)

    if args.known:
        today = date.today().isoformat()
        for key, r in zip(keys, results):
            old = known.get(key, {})
            if r.notes.startswith("skipped:"):
                continue  # not scanned this run: leave the entry as it is
            if r.status not in GOOD_STATUSES and old and not needs_manual(old):
                continue  # a failure never overwrites a working saved entry
            if r.source == "known" and old.get("status") == "listings_found":
                method = old.get("method", "")
            elif r.source == "known" and r.status == "listings_found":
                method = "manual (known_sites.csv)"  # the user pasted a board into a needs-manual entry
            else:
                method = r.method
            verified = r.status == "listings_found" or (r.status in GOOD_STATUSES and bool(r.listings_url))
            known[key] = {"key": key, "company": r.company or old.get("company", ""), "input_url": r.input_url,
                          "status": r.status, "careers_url": r.careers_url or old.get("careers_url", ""),
                          "listings_url": r.listings_url, "ats": r.ats, "method": method,
                          "last_verified": today if verified else old.get("last_verified", ""),
                          "last_checked": today, "manual_check": r.manual_check}
        save_known(args.known, known)
        n_manual = sum(1 for e in known.values() if needs_manual(e))
        log.info("Saved %d known sites to %s (%d need a manual check - see the manual_check column)",
                 len(known), args.known, n_manual)

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
