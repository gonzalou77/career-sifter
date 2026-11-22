"""
ats_clients.py - pull job postings from applicant-tracking-system (ATS) endpoints.

Each client takes the job-board URL that find_careers.py found and returns Job
records via list_jobs(). Listing calls are cheap and usually carry location;
enrich() fetches one job's full description (and anything else the listing lacked).

These are the same public JSON endpoints each company's careers page calls from the
browser. Supported: Workday, Greenhouse, Lever, Ashby, SmartRecruiters, Eightfold, Phenom,
iCIMS, Attrax, Oracle Recruiting Cloud and PeopleAdmin (see CLIENTS at the bottom).
To add a platform, see the ATSClient docstring.
"""
from __future__ import annotations

import html as htmllib
import json
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib import robotparser
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

from classify import is_us_country, parse_location, state_abbr
from find_careers import describe_rejection
from job_details import extract_salary, salary_fields


@dataclass
class Job:
    """One job posting, as collected from a job board and then tagged.

    Fields come in three groups, filled at different stages:

    1. Listing / detail data, set by an ATSClient: ids, title, url, locations, is_us,
       states, city, remote, department, employment_type, posted, level_raw, description.
       ``is_us`` is True / False, or None when the location can't be told yet (enrich()
       usually settles it).
    2. Classification, set by fetch_jobs.classify_job() using classify.py: education,
       years of experience, career level and the keywords matched.
    3. Details, set by job_details.py: salary_* fields, work arrangement, schedule, term,
       shifts, travel, visa, relocation, bonus, equity, certifications.

    ``meta`` is private scratch space for the client (e.g. the Workday job path that
    enrich() needs, or salary / arrangement taken from the board's own fields); it is
    not written to the CSVs or the database.

    To add a field: add it here with a default. database.open_db() adds the column to an
    existing database automatically (list fields need adding to database.LIST_COLUMNS,
    numeric ones to FLOAT_COLUMNS / INT_COLUMNS); add it to ``job_cols`` in
    fetch_jobs.write_outputs() to show it in jobs.csv.
    """
    company: str
    ats: str
    job_id: str
    title: str
    url: str = ""
    locations: list[str] = field(default_factory=list)
    country: str = ""
    is_us: bool | None = None
    states: list[str] = field(default_factory=list)
    city: str = ""
    remote: bool = False
    department: str = ""
    employment_type: str = ""
    posted: str = ""
    level_raw: str = ""
    description: str = ""
    # filled in by classify step
    education_mentioned: list[str] = field(default_factory=list)
    min_education: str = ""
    min_years_experience: int | None = None
    career_level: str = ""
    level_source: str = ""
    keywords: list[str] = field(default_factory=list)
    # details pulled from the posting (job_details.py); empty when the posting doesn't say
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str = ""
    salary_period: str = ""          # hour | week | month | year
    salary_annual_mid: float | None = None  # midpoint per year (hourly x 2080), for comparing
    salary_source: str = ""          # job board | description
    work_arrangement: str = ""       # remote | hybrid | on-site
    schedule: str = ""               # full-time | part-time
    employment_term: str = ""        # permanent | fixed-term | contract | internship
    contract_months: int | str = ""
    shifts: str = ""
    travel: str = ""                 # "up to 25%" | none | required
    visa_sponsorship: str = ""       # offered | not offered
    relocation: str = ""             # offered | possible ("may be available") | not offered
    bonus: str = ""
    equity: str = ""
    certifications: list[str] = field(default_factory=list)
    detail_error: str = ""
    # client-private data needed by enrich()
    meta: dict = field(default_factory=dict, repr=False)


def html_to_text(html: str) -> str:
    """Turn an HTML fragment (a job description) into plain text, one block per line,
    with runs of blank lines collapsed. Returns "" for empty input.
    """
    if not html:
        return ""
    text = BeautifulSoup(html, "html.parser").get_text("\n")
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def structured_salary(low, high, currency, interval) -> dict:
    """Salary from a job board's own pay fields (Ashby, Lever, schema.org baseSalary)."""
    try:
        low, high = float(low), float(high if high is not None else low)
    except (TypeError, ValueError):
        return {}
    iv = str(interval or "").lower()
    period = next((p for p in ("hour", "week", "month", "year") if p in iv), "year")
    return salary_fields(min(low, high), max(low, high), (currency or "USD").upper(), period, "job board")


def jobposting_ld(html: str) -> dict | None:
    """Return the schema.org JobPosting embedded as JSON-LD in a job page, if any."""
    for blob in re.findall(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", html, re.S | re.I):
        try:
            data = json.loads(blob.strip())
        except ValueError:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for item in items:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


def apply_jobposting(job: Job, data: dict) -> None:
    """Fill description, dates and location from a schema.org JobPosting."""
    job.description = html_to_text(data.get("description", "")) or job.description
    emp = data.get("employmentType", "")
    job.employment_type = (", ".join(emp) if isinstance(emp, list) else emp) or job.employment_type
    job.posted = (data.get("datePosted") or "")[:10] or job.posted
    places = data.get("jobLocation") or []
    addrs = [(pl.get("address") or {}) for pl in (places if isinstance(places, list) else [places]) if isinstance(pl, dict)]
    addrs = [a for a in addrs if isinstance(a, dict)]
    if addrs:
        country = [a.get("addressCountry", "") if isinstance(a.get("addressCountry"), str)
                   else (a.get("addressCountry") or {}).get("name", "") for a in addrs]
        apply_location(job, job.locations, country,
                       [a.get("addressRegion", "") for a, c in zip(addrs, country) if is_us_country(c)],
                       city=next((a.get("addressLocality", "") for a in addrs
                                  if a.get("addressLocality") not in (None, "", "UNAVAILABLE")), ""))
    if "TELECOMMUTE" in str(data.get("jobLocationType", "")).upper():
        job.remote = True
        job.meta.setdefault("arrangement", "remote")
    base = data.get("baseSalary") or {}
    if isinstance(base, dict) and not job.meta.get("salary"):
        value = base.get("value") if isinstance(base.get("value"), dict) else {"value": base.get("value")}
        if (sal := structured_salary(value.get("minValue", value.get("value")), value.get("maxValue"),
                                     base.get("currency"), value.get("unitText") or base.get("unitText"))):
            job.meta["salary"] = sal


def apply_location(job: Job, texts: list[str], countries: list[str] = (), regions: list[str] = (),
                   city: str = "") -> None:
    """Set job.locations / is_us / states / city / remote from raw location data."""
    texts = list(dict.fromkeys(t.strip() for t in texts if t and t.strip()))
    job.locations = texts or job.locations
    infos = [parse_location(t) for t in texts]
    country_flags = [is_us_country(c) for c in countries if c]
    text_flags = [i.is_us for i in infos]
    if any(country_flags) or any(f is True for f in text_flags):
        is_us = True
    elif country_flags:  # structured countries are known and none is the US
        is_us = False
    elif text_flags and all(f is False for f in text_flags):
        is_us = False
    else:
        is_us = None
    job.is_us = True if job.is_us else is_us  # never downgrade a server-side US match
    states = [state_abbr(r) for r in regions if state_abbr(r)] + [s for i in infos for s in i.states]
    job.states = list(dict.fromkeys(states)) if job.is_us else []
    job.city = city or job.city or next((i.city for i in infos if i.city), "")
    job.city = re.sub(r",\s*[A-Z]{2}$", "", job.city)  # "North Chicago, IL" -> "North Chicago" (state is in states)
    if not job.city and job.is_us and infos and infos[0].is_us is None and not infos[0].remote:
        job.city = texts[0]  # bare site name such as "Lincoln" or "Kalamazoo - Portage Road"
    job.remote = job.remote or any(i.remote for i in infos)
    job.country = job.country or next((c for c in countries if c), "")


class RobotsDisallowed(Exception):
    """The host's robots.txt asks scripts like ours not to fetch this URL."""


ROBOTS_AGENT = "CareerSifter"
# robots.txt per host, shared by every company in a run: many companies sit on the same job-board
# hosts (api.lever.co, boards-api.greenhouse.io, api.smartrecruiters.com), so each is fetched once
_ROBOTS_CACHE: dict[str, robotparser.RobotFileParser | None] = {}
_ROBOTS_LOCKS: dict[str, threading.Lock] = {}
_ROBOTS_GUARD = threading.Lock()
# a listing that returns fewer than this share of the jobs the board reported is treated as cut
# short (an empty page mid-way, a timeout), so the database won't mark the missing jobs closed
COMPLETE_FRACTION = 0.95


class ATSClient:
    """Base class for one job-board platform (applicant-tracking system).

    A subclass sets ``name`` (must match the ``ats`` value find_careers.py writes, e.g.
    "Workday") and implements list_jobs(); it may override enrich() and finalize().
    fetch_jobs.process_company() drives it:

        client = Cls(board_url, company, session, ...)
        jobs = client.list_jobs(us_only)    # cheap listing calls
        client.enrich(job)                  # per job, in threads, only if a description is missing
        client.finalize(jobs)               # fix-ups that need the whole set

    All HTTP goes through get()/post(), which check robots.txt (raising RobotsDisallowed),
    wait ``delay`` seconds, count refusals in ``rejections`` and raise for HTTP errors.
    Set ``total_openings`` (all locations), ``us_filtered`` (True if the board itself
    filtered to the US) and ``expected_listed`` (the board's own total for the query, so
    listing_complete() can spot a cut-short listing) in list_jobs().

    To add a platform: subclass, implement list_jobs() (and enrich() if the listing has no
    description), add the class to CLIENTS at the bottom of this file, and add its URL
    pattern to ATS_PATTERNS in find_careers.py so boards get detected.
    """
    name = ""

    def __init__(self, board_url: str, company: str, session: requests.Session,
                 delay: float = 0.5, timeout: float = 20, company_domain: str = "",
                 respect_robots: bool = True):
        """Args:
            board_url: the job-board URL find_careers.py found (listings_url).
            company: display name copied onto every Job.
            session: a requests.Session (find_careers.make_session(): honest user agent, retries).
            delay: seconds to wait before every request (politeness; per thread).
            timeout: per-request timeout in seconds.
            company_domain: the company's own domain (e.g. corteva.com), needed by Eightfold;
                "" when the input was a pasted board.
            respect_robots: check robots.txt before each request (False with --ignore-robots).

        Subclasses parse ``board_url`` in their own __init__ and raise ValueError if it lacks
        what they need (site name, board token, ...).
        """
        self.board_url = board_url
        self.company = company
        self.session = session
        self.delay = delay
        self.timeout = timeout
        self.company_domain = company_domain
        self.respect_robots = respect_robots
        self.total_openings: int | None = None   # all locations, if the ATS reports it
        self.us_filtered = False                 # True if list_jobs() returned US jobs only
        self.rejections: Counter[str] = Counter()  # "host 403 Cloudflare" -> times refused
        self._rejections_lock = threading.Lock()  # enrich() runs in several threads
        # how many jobs the board said list_jobs() would return (its own total for the query made);
        # None when the board gives no total (single-request APIs)
        self.expected_listed: int | None = None

    def _check_robots(self, url: str) -> None:
        """Raise RobotsDisallowed if robots.txt on url's host disallows it for our agent.

        robots.txt is fetched once per host per run and shared by all clients and threads
        (module-level _ROBOTS_CACHE, one lock per host). Does nothing when respect_robots is off.
        """
        if not self.respect_robots:
            return
        p = urlparse(url)
        root = f"{p.scheme}://{p.netloc}"
        if root not in _ROBOTS_CACHE:
            with _ROBOTS_GUARD:
                lock = _ROBOTS_LOCKS.setdefault(root, threading.Lock())
            with lock:  # one fetch per host even when several companies ask at once
                if root not in _ROBOTS_CACHE:
                    _ROBOTS_CACHE[root] = self._fetch_robots(root)
        rp = _ROBOTS_CACHE[root]
        if rp is not None and not rp.can_fetch(ROBOTS_AGENT, url):
            raise RobotsDisallowed(f"robots.txt on {p.netloc} disallows {p.path}")

    def _fetch_robots(self, root: str) -> robotparser.RobotFileParser | None:
        """Download and parse root + "/robots.txt".

        Follows RFC 9309: 200 -> parse the rules; 5xx -> treat everything as disallowed; any
        other status (404, 403, ...) -> everything allowed. Returns None when the host can't be
        reached at all, which _check_robots() treats as allowed.
        """
        try:
            r = self.session.get(root + "/robots.txt", timeout=self.timeout)
        except requests.RequestException:
            return None  # unreachable: don't block on it
        rp = robotparser.RobotFileParser()
        if r.status_code == 200:
            rp.parse(r.text.splitlines())
        elif r.status_code >= 500:  # RFC 9309: server error -> assume disallowed
            rp.disallow_all = True
        else:                       # missing -> allowed
            rp.allow_all = True
        return rp

    def listing_complete(self, n_listed: int) -> bool:
        """Did list_jobs() get (nearly) every job the board said it has?"""
        return self.expected_listed is None or n_listed >= COMPLETE_FRACTION * self.expected_listed

    def _request(self, method: str, url: str, **kw) -> requests.Response:
        """Make one HTTP request: robots.txt check, politeness delay, refusal bookkeeping.

        A refusal (403 from Cloudflare, 429, ...; see find_careers.describe_rejection) is counted
        in ``self.rejections`` under "host status vendor" so the summary can report it. Raises
        RobotsDisallowed, requests.HTTPError for 4xx/5xx, or requests.RequestException.
        """
        self._check_robots(url)
        time.sleep(self.delay)
        headers = {"Accept": "application/json, text/html;q=0.9", **kw.pop("headers", {})}
        r = self.session.request(method, url, timeout=self.timeout, headers=headers, **kw)
        label = describe_rejection(r)
        if label:
            with self._rejections_lock:
                self.rejections[f"{urlparse(r.url).netloc} {label}"] += 1
        r.raise_for_status()
        return r

    def get(self, url: str, **kw) -> requests.Response:
        """GET url through _request() (robots.txt, delay, refusal tracking, raise on error)."""
        return self._request("GET", url, **kw)

    def post(self, url: str, **kw) -> requests.Response:
        """POST url through _request() (robots.txt, delay, refusal tracking, raise on error)."""
        return self._request("POST", url, **kw)

    def list_jobs(self, us_only: bool = True) -> list[Job]:
        """Return every job on the board as Job records (subclasses must implement).

        Args:
            us_only: the caller only wants US jobs. Boards with a server-side country filter
                (Workday) use it and set ``us_filtered``; others return everything and leave
                is_us for fetch_jobs to filter on.

        Should set ``total_openings`` and, when the board reports its own total,
        ``expected_listed``. Raise on failure; fetch_jobs records it as blocked / error.
        """
        raise NotImplementedError

    def enrich(self, job: Job) -> None:
        """Fetch the full description. Default: nothing to do (listing already had it)."""

    def finalize(self, jobs: list[Job]) -> None:
        """Hook run after enrich() on all jobs, for fix-ups that need the whole set."""

    def _job(self, job_id, title, **kw) -> Job:
        """Create a Job pre-filled with this client's company and ATS name.

        ``job_id`` is stored as a string (it is part of the database key), the title is
        stripped, and any other Job field can be passed as a keyword.
        """
        return Job(company=self.company, ats=self.name, job_id=str(job_id), title=(title or "").strip(), **kw)

    @staticmethod
    def _segments(url: str) -> list[str]:
        """Non-empty path segments of url: "https://x.com/en-US/Site/" -> ["en-US", "Site"]."""
        return [s for s in urlparse(url).path.split("/") if s]


# --------------------------------------------------------------------------- #

class WorkdayClient(ATSClient):
    """Workday boards (<tenant>.wdN.myworkdayjobs.com/<Site>), read through the JSON API the
    board's own page uses: POST /wday/cxs/<tenant>/<Site>/jobs (20 jobs per page) and
    GET /wday/cxs/<tenant>/<Site><externalPath> for one job's details.

    With us_only, the board's own country facet is applied, so only US jobs are listed
    (``us_filtered``). finalize() uses the state facet to place jobs whose location is
    a bare site name ("Lincoln") in a state.
    """
    name = "Workday"
    PAGE = 20  # Workday's maximum page size

    def __init__(self, *a, **kw):
        """Parse the tenant and site name from the board URL (an optional locale segment such
        as /en-US/ is skipped). Raises ValueError if the URL has no site name.
        """
        super().__init__(*a, **kw)
        p = urlparse(self.board_url)
        segs = self._segments(self.board_url)
        if segs and re.fullmatch(r"[a-z]{2}(-[A-Za-z]{2})?", segs[0]):
            segs = segs[1:]
        if not segs:
            raise ValueError(f"no Workday site name in {self.board_url}")
        self.site = segs[0]
        tenant = p.netloc.split(".")[0]
        self.api = f"https://{p.netloc}/wday/cxs/{tenant}/{self.site}"
        self.web = f"https://{p.netloc}/en-US/{self.site}"

    def _page(self, offset: int, facets: dict) -> dict:
        """Fetch one page (``PAGE`` jobs from ``offset``) of the listing with the given facets
        applied, e.g. {"locationCountry": ["<US facet id>"]}. Returns the raw JSON.
        """
        body = {"appliedFacets": facets, "limit": self.PAGE, "offset": offset, "searchText": ""}
        return self.post(self.api + "/jobs", json=body).json()

    @staticmethod
    def _walk_facets(facets: list):
        """Yield (facetParameter, value) pairs, descending into nested facet groups."""
        for f in facets:
            param = f.get("facetParameter", "")
            for v in f.get("values", []):
                if "facetParameter" in v:
                    yield from WorkdayClient._walk_facets([v])
                else:
                    yield param, v

    def _us_facet(self, facets: list) -> tuple[str, str] | tuple[None, None]:
        """Find the country facet value for the United States in a listing's facets.

        Returns (facetParameter, value id), or (None, None) if the board has no country facet.
        """
        for param, v in self._walk_facets(facets):
            if "country" in param.lower() and is_us_country(v.get("descriptor", "")):
                return param, v["id"]
        return None, None

    def _all_postings(self, facets: dict, first: dict | None = None) -> list[dict]:
        """Page through the whole listing for the given facets and return the raw postings.

        Workday only reports ``total`` reliably on the first page, so that number drives the
        loop; it also stops at the first empty page. ``first`` is an already-fetched page 0.
        """
        data = first or self._page(0, facets)
        total = data.get("total", 0)
        postings = list(data.get("jobPostings", []))
        while len(postings) < total:
            batch = self._page(len(postings), facets).get("jobPostings", [])
            if not batch:
                break
            postings += batch
        return postings

    def list_jobs(self, us_only=True):
        """List all jobs (US only when the board has a country facet and us_only is set).

        ``total_openings`` is the unfiltered worldwide total; ``expected_listed`` is the total
        for the query actually made. A job's id is its first bullet field (the requisition id),
        falling back to its path. "N Locations" listing text is left for enrich() to resolve.
        """
        first = self._page(0, {})
        self.total_openings = first.get("total", 0)
        self.facets, self.facet_data = {}, first
        if us_only:
            param, fid = self._us_facet(first.get("facets", []))
            if param:
                self.facets, self.us_filtered = {param: [fid]}, True
                self.facet_data = self._page(0, self.facets)
        postings = self._all_postings(self.facets, self.facet_data)
        self.expected_listed = self.facet_data.get("total", 0)

        jobs = []
        for p in postings:
            path = p.get("externalPath", "")
            job = self._job((p.get("bulletFields") or [path])[0], p.get("title"),
                            url=self.web + path, posted=p.get("postedOn", ""), meta={"path": path})
            if self.us_filtered:
                job.is_us = True
            loc = p.get("locationsText", "")
            if loc and not re.fullmatch(r"\d+ Locations", loc):
                apply_location(job, [loc])
            jobs.append(job)
        return jobs

    def enrich(self, job):
        """Fetch one job's detail record: description, public URL, time type (full/part time),
        start date and all its locations. The primary country is only applied to the first
        location; additional locations are judged from their own text.
        """
        info = self.get(self.api + job.meta["path"]).json().get("jobPostingInfo", {})
        job.description = html_to_text(info.get("jobDescription", ""))
        job.url = info.get("externalUrl") or job.url
        job.employment_type = info.get("timeType", "")
        job.posted = info.get("startDate") or job.posted
        country = (info.get("country") or {}).get("descriptor", "")
        locs = [info.get("location", "")] + list(info.get("additionalLocations") or [])
        # primary country only describes the first location; let the strings decide the rest
        apply_location(job, locs, [country] if is_us_country(country) else [])

    def finalize(self, jobs):
        """Many tenants use bare site names ("Lincoln", "Kalamazoo - Portage St") with no state.
        Ask the board's own state facet which jobs sit in which state."""
        missing = {j.meta["path"]: j for j in jobs if j.is_us and not j.states}
        if not missing or not self.us_filtered:
            return
        # each state costs a full listing of its jobs, so smallest states first (cheapest to read)
        # and stop as soon as every job has a state. A multi-state job keeps the first state found.
        states = [(param, v, state_abbr(v.get("descriptor", "")))
                  for param, v in self._walk_facets(self.facet_data.get("facets", []))
                  if re.search(r"state|region|province", param, re.I) and v.get("count")]
        for param, v, ab in sorted((s for s in states if s[2]), key=lambda s: s[1].get("count", 0)):
            for p in self._all_postings({**self.facets, param: [v["id"]]}):
                if (j := missing.pop(p.get("externalPath"), None)):
                    j.states.append(ab)
            if not missing:
                return


class GreenhouseClient(ATSClient):
    """Greenhouse boards (boards.greenhouse.io/<token>, job-boards.greenhouse.io/<token>, or an
    embed URL with ?for=<token>), read from the public board API in one request:
    GET boards-api.greenhouse.io/v1/boards/<token>/jobs?content=true, which already includes
    each job's description, so no enrich() is needed.
    """
    name = "Greenhouse"

    def __init__(self, *a, **kw):
        """Take the board token from ?for=<token> (embed URLs) or the first path segment.
        Raises ValueError if neither is present.
        """
        super().__init__(*a, **kw)
        q = parse_qs(urlparse(self.board_url).query)
        segs = self._segments(self.board_url)
        self.token = (q.get("for") or [None])[0] or (segs[0] if segs and segs[0] != "embed" else None)
        if not self.token:
            raise ValueError(f"no Greenhouse board token in {self.board_url}")

    def list_jobs(self, us_only=True):
        """List every job with its description, departments and office locations.
        Greenhouse has no country filter, so ``us_only`` is ignored here.
        """
        data = self.get(f"https://boards-api.greenhouse.io/v1/boards/{self.token}/jobs",
                        params={"content": "true"}).json()
        jobs = []
        for j in data.get("jobs", []):
            job = self._job(j.get("id"), j.get("title"), url=j.get("absolute_url", ""),
                            posted=j.get("updated_at", ""),
                            department=", ".join(d.get("name", "") for d in j.get("departments", [])),
                            description=html_to_text(htmllib.unescape(j.get("content", ""))))
            locs = [(j.get("location") or {}).get("name", "")]
            locs += [o.get("location") or o.get("name", "") for o in j.get("offices", [])]
            apply_location(job, locs)
            jobs.append(job)
        self.total_openings = len(jobs)
        return jobs


class LeverClient(ATSClient):
    """Lever boards (jobs.lever.co/<company>, or jobs.eu.lever.co for EU-hosted accounts), read
    from the public postings API in one request: GET api.lever.co/v0/postings/<company>?mode=json.
    Descriptions, workplace type and the salary range are all in the listing.
    """
    name = "Lever"

    def __init__(self, *a, **kw):
        """Take the company slug from the first path segment and pick the EU or US API host.
        Raises ValueError if the URL has no slug.
        """
        super().__init__(*a, **kw)
        segs = self._segments(self.board_url)
        if not segs:
            raise ValueError(f"no Lever company in {self.board_url}")
        self.slug = segs[0]
        self.api = "https://api.eu.lever.co" if ".eu." in urlparse(self.board_url).netloc else "https://api.lever.co"

    def list_jobs(self, us_only=True):
        """List every job. The description is assembled from the plain-text intro, each list
        section (heading plus items) and the closing text. Lever's salaryRange and
        workplaceType are kept in meta so they win over anything read from the text.
        """
        data = self.get(f"{self.api}/v0/postings/{self.slug}", params={"mode": "json"}).json()
        jobs = []
        for j in data:
            cat = j.get("categories") or {}
            parts = [j.get("descriptionPlain", "")]
            for lst in j.get("lists", []):
                parts += [lst.get("text", ""), html_to_text(lst.get("content", ""))]
            parts.append(j.get("additionalPlain", ""))
            created = j.get("createdAt")
            job = self._job(j.get("id"), j.get("text"), url=j.get("hostedUrl", ""),
                            department=cat.get("team", ""), employment_type=cat.get("commitment", ""),
                            posted=datetime.fromtimestamp(created / 1000, timezone.utc).date().isoformat()
                            if created else "",
                            description="\n".join(p for p in parts if p),
                            remote=(j.get("workplaceType") == "remote"))
            apply_location(job, [cat.get("location", "")] + list(cat.get("allLocations") or []),
                           [j.get("country", "")])
            job.meta["arrangement"] = j.get("workplaceType", "")
            sr = j.get("salaryRange") or {}  # {"min", "max", "currency", "interval": "per-year-salary"}
            if (sal := structured_salary(sr.get("min"), sr.get("max"), sr.get("currency"), sr.get("interval"))):
                job.meta["salary"] = sal
            jobs.append(job)
        self.total_openings = len(jobs)
        return jobs


class AshbyClient(ATSClient):
    """Ashby boards (jobs.ashbyhq.com/<org>), read from the public posting API in one request:
    GET api.ashbyhq.com/posting-api/job-board/<org>?includeCompensation=true. The listing has
    descriptions, structured addresses and compensation, so no enrich() is needed.
    """
    name = "Ashby"

    def __init__(self, *a, **kw):
        """Take the organisation slug from the first path segment. Raises ValueError if missing."""
        super().__init__(*a, **kw)
        segs = self._segments(self.board_url)
        if not segs:
            raise ValueError(f"no Ashby organisation in {self.board_url}")
        self.org = segs[0]

    def list_jobs(self, us_only=True):
        """List every listed job (``isListed`` False ones are skipped). Locations use the
        structured postal addresses (country, region, city) of the primary and secondary
        locations; the first "Salary" compensation component becomes the job's salary.
        """
        data = self.get(f"https://api.ashbyhq.com/posting-api/job-board/{self.org}",
                        params={"includeCompensation": "true"}).json()
        jobs = []
        for j in data.get("jobs", []):
            if j.get("isListed") is False:
                continue
            addrs = [((j.get("address") or {}).get("postalAddress") or {})]
            addrs += [((s.get("address") or {}).get("postalAddress") or {}) for s in j.get("secondaryLocations", [])]
            job = self._job(j.get("id"), j.get("title"), url=j.get("jobUrl", ""),
                            posted=(j.get("publishedAt") or "")[:10], department=j.get("department", ""),
                            employment_type=j.get("employmentType", ""),
                            description=j.get("descriptionPlain", ""),
                            remote=bool(j.get("isRemote")) or j.get("workplaceType") == "Remote")
            apply_location(job, [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations", [])],
                           [a.get("addressCountry", "") for a in addrs],
                           [a.get("addressRegion", "") for a in addrs],
                           city=addrs[0].get("addressLocality", ""))
            job.meta["arrangement"] = j.get("workplaceType", "")
            for comp in ((j.get("compensation") or {}).get("summaryComponents") or []):
                if comp.get("compensationType") == "Salary":
                    if (sal := structured_salary(comp.get("minValue"), comp.get("maxValue"),
                                                 comp.get("currencyCode"), comp.get("interval"))):
                        job.meta["salary"] = sal
                    break
            jobs.append(job)
        self.total_openings = len(jobs)
        return jobs


class SmartRecruitersClient(ATSClient):
    """SmartRecruiters boards (jobs.smartrecruiters.com/<Company>), read from the public postings
    API: GET api.smartrecruiters.com/v1/companies/<Company>/postings (100 per page) and
    .../postings/<id> for details.

    Note: SmartRecruiters' robots.txt disallows this API, so by default the company is
    reported as robots_disallowed and nothing is collected (see --ignore-robots).
    """
    name = "SmartRecruiters"

    def __init__(self, *a, **kw):
        """Take the company id from the URL; an employee SSO link (/web-sso/saml/<Company>/...)
        also works. Raises ValueError if no company id is found.
        """
        super().__init__(*a, **kw)
        segs = self._segments(self.board_url)
        if segs[:2] == ["web-sso", "saml"] and len(segs) > 2:  # employee SSO link: /web-sso/saml/<Company>/login
            segs = segs[2:]
        if not segs:
            raise ValueError(f"no SmartRecruiters company in {self.board_url}")
        self.cid = segs[0]
        self.api = f"https://api.smartrecruiters.com/v1/companies/{self.cid}/postings"

    def list_jobs(self, us_only=True):
        """Page through all postings. The board's experienceLevel label is kept in level_raw
        (used for career level when the title says nothing).
        """
        jobs, offset, total = [], 0, None
        while total is None or offset < total:
            data = self.get(self.api, params={"limit": 100, "offset": offset}).json()
            total = data.get("totalFound", 0)
            batch = data.get("content", [])
            if not batch:
                break
            offset += len(batch)
            for j in batch:
                loc = j.get("location") or {}
                job = self._job(j.get("id"), j.get("name"),
                                url=f"https://jobs.smartrecruiters.com/{self.cid}/{j.get('id')}",
                                posted=(j.get("releasedDate") or "")[:10],
                                department=(j.get("department") or {}).get("label", ""),
                                employment_type=(j.get("typeOfEmployment") or {}).get("label", ""),
                                level_raw=(j.get("experienceLevel") or {}).get("label", ""),
                                remote=bool(loc.get("remote")))
                apply_location(job, [loc.get("fullLocation") or ", ".join(
                    filter(None, [loc.get("city"), loc.get("region"), loc.get("country")]))],
                    [loc.get("country", "")], [loc.get("region", "")], city=loc.get("city", ""))
                jobs.append(job)
        self.total_openings = self.expected_listed = total
        return jobs

    def enrich(self, job):
        """Fetch one posting and join the text of its job-ad sections into the description."""
        d = self.get(f"{self.api}/{job.job_id}").json()
        sections = ((d.get("jobAd") or {}).get("sections") or {}).values()
        job.description = "\n".join(html_to_text(s.get("text", "")) for s in sections if isinstance(s, dict))
        job.url = d.get("postingUrl") or job.url


class EightfoldClient(ATSClient):
    """Eightfold career sites (<company>.eightfold.ai/careers?domain=<company domain>), read from
    the site's JSON API: /api/pcsx/search (paged by ``start``) and /api/pcsx/position_details.

    The API needs the company's domain, taken from ?domain= in the board URL or from
    ``company_domain``. Note: Eightfold's robots.txt disallows /api/, so by default these
    companies are reported as robots_disallowed.
    """
    name = "Eightfold"

    def __init__(self, *a, **kw):
        """Read the host and the company domain (?domain=, else company_domain).
        Raises ValueError if no domain is available.
        """
        super().__init__(*a, **kw)
        p = urlparse(self.board_url)
        self.host = p.netloc
        self.domain = (parse_qs(p.query).get("domain") or [self.company_domain])[0]
        if not self.domain:
            raise ValueError("Eightfold needs the company domain")

    def list_jobs(self, us_only=True):
        """Page through all positions. Countries come from standardizedLocations
        ("Iowa,US" -> "US"); a job whose standardized locations name no US place is marked
        non-US rather than unknown.
        """
        jobs, start, total = [], 0, None
        while total is None or start < total:
            data = self.get(f"https://{self.host}/api/pcsx/search",
                            params={"domain": self.domain, "query": "", "location": "", "start": start}
                            ).json().get("data") or {}
            total = data.get("count", 0)
            batch = data.get("positions", [])
            if not batch:
                break
            start += len(batch)
            for p in batch:
                job = self._job(p.get("id"), p.get("name"),
                                url=f"https://{self.host}{p.get('positionUrl', '')}",
                                department=p.get("department", ""),
                                posted=datetime.fromtimestamp(p["postedTs"], timezone.utc).date().isoformat()
                                if p.get("postedTs") else "",
                                remote=(p.get("workLocationOption") == "remote"))
                # standardizedLocations look like "Iowa,US" / "Lombardy,IT"
                std = p.get("standardizedLocations") or []
                apply_location(job, p.get("locations") or [], [s.rsplit(",", 1)[-1] for s in std])
                job.meta["arrangement"] = p.get("workLocationOption") or ""
                if std and job.is_us is None:
                    job.is_us = False
                jobs.append(job)
        self.total_openings = self.expected_listed = total
        return jobs

    def enrich(self, job):
        """Fetch one position's details: description and public URL."""
        d = self.get(f"https://{self.host}/api/pcsx/position_details",
                     params={"position_id": job.job_id, "domain": self.domain, "hl": "en"}
                     ).json().get("data") or {}
        job.description = html_to_text(d.get("jobDescription", ""))
        job.url = d.get("publicUrl") or job.url


class PhenomClient(ATSClient):
    """Phenom career sites embed their search results as JSON (phApp.ddo) in the page."""
    name = "Phenom"
    DDO_RE = re.compile(r"phApp\.ddo\s*=\s*(\{.*?\});\s*phApp\.", re.S)

    def __init__(self, *a, **kw):
        """Work out the site's /<country>/<lang> base (e.g. careers.gene.com/us/en); defaults
        to /us/en when the board URL doesn't include one.
        """
        super().__init__(*a, **kw)
        p = urlparse(self.board_url)
        segs = self._segments(self.board_url)
        # career sites live under /<country>/<lang>, e.g. careers.gene.com/us/en
        locale = segs[:2] if len(segs) >= 2 and all(len(s) == 2 for s in segs[:2]) else ["us", "en"]
        self.base = f"{p.scheme}://{p.netloc}/" + "/".join(locale)

    def _ddo(self, url: str) -> dict:
        """Fetch a Phenom page and return its embedded ``phApp.ddo`` JSON (the data the page's
        own JavaScript renders from). Raises ValueError if the page has none.
        """
        m = self.DDO_RE.search(self.get(url, headers={"Accept": "text/html"}).text)
        if not m:
            raise ValueError(f"no Phenom data on {url}")
        return json.loads(m.group(1))

    def list_jobs(self, us_only=True):
        """Page through /search-results?from=N, reading the jobs and the total from the
        embedded data. The site's jobLevel label goes into level_raw.
        """
        jobs, start, total = [], 0, None
        while total is None or start < total:
            rs = self._ddo(f"{self.base}/search-results?from={start}&s=1").get("eagerLoadRefineSearch") or {}
            total = rs.get("totalHits", 0)
            batch = (rs.get("data") or {}).get("jobs", [])
            if not batch:
                break
            start += len(batch)
            for j in batch:
                jid = j.get("jobId") or j.get("jobSeqNo")
                job = self._job(jid, j.get("title"), url=f"{self.base}/job/{jid}",
                                posted=(j.get("postedDate") or "")[:10], department=j.get("category", ""),
                                employment_type=j.get("type", ""), level_raw=j.get("jobLevel") or "")
                apply_location(job, j.get("multi_location") or [j.get("location", "")],
                               [j.get("country", "")], [j.get("state", "")], city=j.get("city", ""))
                jobs.append(job)
        self.total_openings = self.expected_listed = total
        return jobs

    def enrich(self, job):
        """Fetch one job page and take the description (and job level, if given) from its
        embedded data.
        """
        jd = (((self._ddo(job.url).get("jobDetail") or {}).get("data") or {}).get("job") or {})
        job.description = html_to_text(jd.get("description", ""))
        job.level_raw = jd.get("jobLevel") or job.level_raw


class ICIMSClient(ATSClient):
    """iCIMS comes in two flavours:

    - classic portals (careers-<co>.icims.com): HTML search pages, 20 jobs per page,
      each job page carrying schema.org JobPosting JSON-LD;
    - iCIMS career sites (e.g. careers.certara.com): a JSON API at /api/jobs.

    A classic portal that redirects to a career site is followed automatically.
    """
    name = "iCIMS"
    REDIRECT_RE = re.compile(r"window\.top\.location\.href\s*=\s*'([^']+)'")
    ROW_LOC_RE = re.compile(r"^([A-Z]{2})-(?:([A-Z]{2})-)?(.*)$")  # "US-GA-Norcross", "ES-Barcelona"

    def __init__(self, *a, **kw):
        """Pick the mode from the host: *.icims.com is a classic portal, anything else an
        iCIMS career site with a JSON API.
        """
        super().__init__(*a, **kw)
        p = urlparse(self.board_url)
        self.host = p.netloc
        self.mode = "classic" if self.host.endswith(".icims.com") else "site"
        self.base = f"https://{self.host}"

    def _search_page(self, page: int) -> str:
        """Fetch page ``page`` (0-based) of a classic portal's job search, in its iframe form."""
        return self.get(f"{self.base}/jobs/search", params={"ss": 1, "in_iframe": 1, "pr": page},
                        headers={"Accept": "text/html"}).text

    def list_jobs(self, us_only=True):
        """List jobs in whichever mode applies. A classic portal whose search page redirects
        (window.top.location) to a career site on another domain is switched to site mode.
        """
        if self.mode == "classic":
            first = self._search_page(0)
            m = self.REDIRECT_RE.search(first)
            target = urlparse(m.group(1).replace("\\/", "/")).netloc if m else ""
            if target and not target.endswith(".icims.com"):
                self.mode, self.host, self.base = "site", target, f"https://{target}"
            else:
                return self._list_classic(first)
        return self._list_site()

    # ---- classic portal ---------------------------------------------------- #

    def _list_classic(self, first_html: str) -> list[Job]:
        """Read every search page of a classic portal and turn each table row into a Job.

        The page count comes from "Page X of N", else from the highest pr= link. Locations are
        the row's "US-ST-City" codes. Descriptions need enrich() (one request per job).
        """
        pages = [first_html]
        m = re.search(r"Page\s+\d+\s+of\s+(\d+)", BeautifulSoup(first_html, "html.parser").get_text(" "))
        n_pages = int(m.group(1)) if m else 1 + max(map(int, re.findall(r"[?&;]pr=(\d+)", first_html)), default=0)
        for page in range(1, n_pages):
            pages.append(self._search_page(page))

        jobs, seen = [], set()
        for html in pages:
            for row in BeautifulSoup(html, "html.parser").select(".iCIMS_JobsTable .row"):
                a = row.select_one("a.iCIMS_Anchor[href*='/jobs/']")
                jm = re.search(r"/jobs/(\d+)/", a["href"]) if a else None
                if not jm or jm.group(1) in seen:
                    continue
                seen.add(jm.group(1))
                title_el = a.select_one("h3") or a
                url = re.sub(r"[?&]in_iframe=1", "", a["href"])
                job = self._job(jm.group(1), title_el.get_text(" ", strip=True), url=url)
                loc_el = row.select_one(".header.left span:not(.sr-only)")
                self._row_location(job, loc_el.get_text("\n", strip=True) if loc_el else "")
                jobs.append(job)
        self.total_openings = len(jobs)
        return jobs

    def _row_location(self, job: Job, text: str) -> None:
        """Rows use "CC-ST-City" (country, state, city); several locations are newline-separated."""
        locs = [t.strip() for t in re.split(r"[\n|]", text) if t.strip()]
        countries, regions, city = [], [], ""
        for loc in locs:
            m = self.ROW_LOC_RE.match(loc)
            if m:
                countries.append(m.group(1))
                if m.group(1) == "US" and m.group(2):
                    regions.append(m.group(2))
                    city = city or m.group(3)
        apply_location(job, locs, countries, regions, city=city)

    # ---- career site API --------------------------------------------------- #

    def _list_site(self) -> list[Job]:
        """Page through a career site's /api/jobs feed (100 per page). Descriptions,
        responsibilities and qualifications are included, so no detail requests are needed.
        Stops when a page brings no job not already seen.
        """
        jobs, seen, page, total = [], set(), 1, None
        while total is None or len(seen) < total:
            data = self.get(f"{self.base}/api/jobs", params={"page": page, "limit": 100}).json()
            total = data.get("totalCount", 0)
            batch = [j.get("data", j) for j in data.get("jobs", [])]
            new = [j for j in batch if (j.get("req_id") or j.get("slug")) not in seen]
            if not new:
                break
            page += 1
            for j in new:
                jid = j.get("req_id") or j.get("slug")
                seen.add(jid)
                desc = "\n".join(html_to_text(j.get(k, "")) for k in ("description", "responsibilities", "qualifications"))
                job = self._job(jid, j.get("title"), url=f"{self.base}/jobs/{j.get('slug') or jid}",
                                posted=(j.get("posted_date") or "")[:10], description=desc.strip(),
                                department=", ".join(c.get("name", "") for c in j.get("categories") or []),
                                employment_type=j.get("employment_type", ""),
                                remote=(j.get("location_type") == "REMOTE"))
                locs = [j.get("location_name") or j.get("full_location") or ""]
                locs += [loc.get("location_name", "") if isinstance(loc, dict) else str(loc)
                         for loc in j.get("additional_locations") or []]
                apply_location(job, locs, [j.get("country_code") or j.get("country", "")],
                               [j.get("state", "")], city=j.get("city", ""))
                jobs.append(job)
        self.total_openings = total or len(jobs)
        self.expected_listed = total or None
        return jobs

    # ---- detail (classic only; the API already includes descriptions) ------ #

    def enrich(self, job):
        """Fetch a classic portal's job page and fill the job from its schema.org JobPosting
        data (description, type, date, location, salary), falling back to the visible job text.
        """
        html = self.get(job.url, params={"in_iframe": 1}, headers={"Accept": "text/html"}).text
        data = jobposting_ld(html)
        if data:
            apply_jobposting(job, data)
            return
        # no JSON-LD: fall back to the visible job content
        content = BeautifulSoup(html, "html.parser").select(".iCIMS_JobContent")
        job.description = "\n".join(c.get_text("\n", strip=True) for c in content)


class AttraxClient(ATSClient):
    """Attrax career sites (e.g. careers.abbvie.com): server-rendered job tiles at /<lang>/jobs,
    each job page carrying schema.org JobPosting JSON-LD."""
    name = "Attrax"
    PAGE_SIZE = 100
    TOTAL_RE = re.compile(r"([\d,]+)\s+result\(s\)", re.I)

    def __init__(self, *a, **kw):
        """Keep the site origin and its language prefix (e.g. /en), from which /jobs is read."""
        super().__init__(*a, **kw)
        p = urlparse(self.board_url)
        segs = self._segments(self.board_url)
        locale = segs[0] if segs and re.fullmatch(r"[a-z]{2}(-[a-z]{2})?", segs[0], re.I) else ""
        self.origin = f"{p.scheme}://{p.netloc}"
        self.base = self.origin + (f"/{locale}" if locale else "")

    def _html(self, url: str, **kw) -> str:
        """GET a page as HTML, decoded as UTF-8 (Attrax labels pages ISO-8859-1 but serves UTF-8)."""
        # Attrax labels pages ISO-8859-1 but serves UTF-8
        return self.get(url, headers={"Accept": "text/html"}, **kw).content.decode("utf-8", "replace")

    def list_jobs(self, us_only=True):
        """Page through /<lang>/jobs (``PAGE_SIZE`` tiles per page) until every job the site's
        "N result(s)" count promised has been seen, or a page brings nothing new.
        """
        jobs, seen, page, total = [], set(), 1, None
        while total is None or len(seen) < total:
            html = self._html(f"{self.base}/jobs", params={"page": page, "size": self.PAGE_SIZE})
            if total is None:
                m = self.TOTAL_RE.search(html)
                total = int(m.group(1).replace(",", "")) if m else 0
            tiles = [t for t in BeautifulSoup(html, "html.parser").select(".attrax-vacancy-tile[data-jobid]")
                     if t["data-jobid"] not in seen]
            if not tiles:
                break
            for t in tiles:
                seen.add(t["data-jobid"])
                jobs.append(self._tile(t))
            page += 1
        self.total_openings = total or len(jobs)
        self.expected_listed = total or None
        return jobs

    def _tile(self, tile) -> Job:
        """Turn one job tile into a Job. Tiles carry labelled fields (Location, Function, Job
        Type, Experience Level, Work location type, Salary); the US is recognised from the
        tile's country CSS class. A salary on the tile counts as the job board's own.
        """
        a = tile.select_one("a.attrax-vacancy-tile__title")
        fields: dict[str, list[str]] = {}
        for label in tile.select(".attrax-vacancy-tile__item-label"):
            item = label.find_parent(class_="attrax-vacancy-tile__item")
            values = [v.get_text(" ", strip=True) for v in item.select(".attrax-vacancy-tile__item-value")] if item else []
            fields.setdefault(label.get_text(strip=True), values)  # first "Location" is the free-text one
        first = lambda k: (fields.get(k) or [""])[0]
        job = self._job(tile["data-jobid"], a.get_text(" ", strip=True) if a else "",
                        url=self.origin + a["href"] if a and a.get("href", "").startswith("/") else (a or {}).get("href", ""),
                        department=first("Function"), employment_type=first("Job Type"),
                        level_raw=first("Experience Level"), remote="remote" in first("Work location type").lower())
        # tiles carry slugged option values as classes, e.g. attrax-vacancy-tile--united-states-of-america
        countries = ["United States"] if "attrax-vacancy-tile--united-states" in " ".join(tile.get("class", [])) else []
        apply_location(job, fields.get("Location", [])[:3], countries)
        job.meta["arrangement"] = first("Work location type")
        if first("Salary") and (sal := extract_salary("Salary " + first("Salary"))):
            sal["salary_source"] = "job board"
            job.meta["salary"] = sal
        return job

    def enrich(self, job):
        """Fetch the job page and fill the job from its schema.org JobPosting data, falling
        back to the visible description block.
        """
        html = self._html(job.url)
        data = jobposting_ld(html)
        if data:
            apply_jobposting(job, data)
        if not job.description:
            content = BeautifulSoup(html, "html.parser").select(".jobad-jobdescription, .description-widget")
            job.description = "\n".join(c.get_text("\n", strip=True) for c in content)


class OracleClient(ATSClient):
    """Oracle Recruiting Cloud ("Candidate Experience") sites, on *.oraclecloud.com or a vanity
    domain such as careersearch.stanford.edu. Uses the site's public recruitingCE REST API."""
    name = "Oracle Cloud HCM"
    PAGE = 100

    def __init__(self, *a, **kw):
        """Read the host, site number (/sites/<id>) and language from the board URL. The site
        may be unknown at this point for a vanity domain; _resolve_site() finds it.
        """
        super().__init__(*a, **kw)
        p = urlparse(self.board_url)
        self.host = p.netloc
        m = re.search(r"/sites/([^/?#]+)", p.path)
        self.site = m.group(1) if m else ""
        m = re.search(r"/CandidateExperience/([a-z]{2}(?:-[a-z]{2})?)/", p.path, re.I)
        self.lang = m.group(1) if m else "en"
        self.api = f"https://{self.host}/hcmRestApi/resources/latest"

    def _resolve_site(self) -> None:
        """Find the site number when the board URL didn't contain one: the vanity root
        redirects to /sites/<id>, or the page mentions siteNumber. Falls back to "CX_1",
        Oracle's default site.
        """
        if self.site:
            return
        r = self.get(self.board_url, headers={"Accept": "text/html"})  # vanity root redirects to /sites/<id>
        m = re.search(r"/sites/([^/?#\"']+)", r.url) or re.search(r"siteNumber[\"':=\s]+([A-Za-z0-9_]+)", r.text)
        self.site = m.group(1) if m else "CX_1"

    def list_jobs(self, us_only=True):
        """Page through recruitingCEJobRequisitions (``PAGE`` per request). Primary and
        secondary locations, their countries and the workplace type come with the listing.
        """
        self._resolve_site()
        jobs, offset, total = [], 0, None
        while total is None or offset < total:
            url = (f"{self.api}/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList.secondaryLocations"
                   f"&finder=findReqs;siteNumber={self.site},limit={self.PAGE},offset={offset}")
            items = self.get(url).json().get("items") or [{}]
            total = items[0].get("TotalJobsCount", 0)
            batch = items[0].get("requisitionList") or []
            if not batch:
                break
            offset += len(batch)
            for r in batch:
                job = self._job(r.get("Id"), r.get("Title"), posted=r.get("PostedDate") or "",
                                url=f"https://{self.host}/hcmUI/CandidateExperience/{self.lang}/sites/{self.site}/job/{r.get('Id')}",
                                remote=r.get("WorkplaceTypeCode") == "ORA_REMOTE")
                second = r.get("secondaryLocations") or []
                apply_location(job, [r.get("PrimaryLocation", "")] + [s.get("Name", "") for s in second],
                               [r.get("PrimaryLocationCountry", "")] + [s.get("CountryCode", "") for s in second])
                job.meta["arrangement"] = r.get("WorkplaceTypeCode") or r.get("WorkplaceType") or ""
                jobs.append(job)
        self.total_openings = self.expected_listed = total
        return jobs

    def enrich(self, job):
        """Fetch one requisition's details: description, responsibilities and qualifications
        joined into the description, plus job level, category and schedule.
        """
        url = (f"{self.api}/recruitingCEJobRequisitionDetails?expand=all&onlyData=true"
               f"&finder=ById;Id=%22{job.job_id}%22,siteNumber={self.site}")
        d = (self.get(url).json().get("items") or [{}])[0]
        job.description = "\n".join(html_to_text(d.get(k) or "") for k in
                                    ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr")).strip()
        job.level_raw = d.get("JobLevel") or job.level_raw
        job.department = d.get("Category") or job.department
        job.employment_type = d.get("JobSchedule") or job.employment_type


class PeopleAdminClient(ATSClient):
    """PeopleAdmin (higher-ed) job sites such as jobs.montana.edu. Listing comes from the
    public Atom feed at /postings/search.atom; details from each posting's field table.

    PeopleAdmin is a US higher-education platform, so postings are counted as US jobs;
    the state is filled in only when a location field names one.
    """
    name = "PeopleAdmin"
    LOCATION_FIELD_RE = re.compile(r"location|campus|city|work site", re.I)

    def __init__(self, *a, **kw):
        """Keep the site origin; the feed and postings live under /postings."""
        super().__init__(*a, **kw)
        p = urlparse(self.board_url)
        self.base = f"{p.scheme}://{p.netloc}"

    def list_jobs(self, us_only=True):
        """Read every posting from the Atom feed. Each job is marked US (PeopleAdmin is a US
        platform) and ``summary_only``, so fetch_jobs still calls enrich() for the full text.
        """
        feed = BeautifulSoup(self.get(f"{self.base}/postings/search.atom",
                                      headers={"Accept": "application/atom+xml"}).content, "html.parser")
        jobs = []
        for e in feed.find_all("entry"):
            link = e.find("link", href=True)
            m = re.search(r"/postings/(\d+)", link["href"]) if link else None
            if not m:  # e.g. the "No Positions Currently Open" placeholder entry
                continue
            author = e.find("author")
            job = self._job(m.group(1), e.find("title").get_text(" ", strip=True) if e.find("title") else "",
                            url=link["href"], posted=(e.find("published").get_text(strip=True) if e.find("published") else "")[:10],
                            department=author.get_text(" ", strip=True) if author else "",
                            description=html_to_text(htmllib.unescape(e.find("content").get_text() if e.find("content") else "")))
            job.is_us = True
            job.meta["summary_only"] = True
            jobs.append(job)
        self.total_openings = len(jobs)
        return jobs

    def enrich(self, job):
        """Fetch a posting page and turn its field table into the description ("Field: value"
        lines). Fields named like location / campus / city set the job's location.
        """
        soup = BeautifulSoup(self.get(job.url, headers={"Accept": "text/html"}).content, "html.parser")
        fields = []
        for tr in soup.select("table tr"):
            th, td = tr.find("th"), tr.find("td")
            if th and td and td.get_text(strip=True):
                fields.append((th.get_text(" ", strip=True), td.get_text("\n", strip=True)))
        if fields:
            job.description = "\n".join(f"{k}: {v}" for k, v in fields)
        locs = [v for k, v in fields if self.LOCATION_FIELD_RE.search(k) and len(v) < 120]
        if locs:
            apply_location(job, locs)


CLIENTS: dict[str, type[ATSClient]] = {
    c.name: c for c in (WorkdayClient, GreenhouseClient, LeverClient, AshbyClient, SmartRecruitersClient,
                        EightfoldClient, PhenomClient, ICIMSClient, AttraxClient, OracleClient, PeopleAdminClient)
}
