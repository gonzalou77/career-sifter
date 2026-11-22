"""Offline checks for the parts not covered by the other test files: link scoring and job-board
detection (find_careers.py), refusal detection, location / schema.org helpers (ats_clients.py),
and a full fetch_jobs.py run against a fake job board, through to the CSVs and the database.
No network access needed. Run: python test_pipeline.py (or python run_tests.py for everything)."""
import argparse
import csv
import os
import sqlite3
import tempfile

import requests
from requests.structures import CaseInsensitiveDict

import database
from ats_clients import (CLIENTS, ATSClient, Job, apply_jobposting, apply_location, jobposting_ld,
                         structured_salary)
from fetch_jobs import keyword_res, process_company, write_outputs
from find_careers import (CAREER_PHRASES, Page, analyze_page, ats_board_url, board_company_name, describe_rejection,
                          detect_ats, extract_candidates, is_excluded_host, score_link)


# --------------------------------------------------------------------------- #
# find_careers.py
# --------------------------------------------------------------------------- #

def test_detect_ats():
    assert detect_ats("jobs.lever.co/acme") == "Lever"
    assert detect_ats("clever.com/jobs") == ""  # "lever.co" inside another word is not Lever
    assert detect_ats("pfizer.wd1.myworkdayjobs.com/pfizercareers") == "Workday"
    assert detect_ats("careersearch.stanford.edu/hcmui/candidateexperience/en/sites/stanford") == "Oracle Cloud HCM"
    assert detect_ats("www.example.com/careers") == ""


def test_ats_board_url():
    cases = {
        "https://pfizer.wd1.myworkdayjobs.com/en-US/PfizerCareers/job/US-NY/Scientist_123":
            "https://pfizer.wd1.myworkdayjobs.com/PfizerCareers",
        "https://jobs.ashbyhq.com/benchling/65d3-abc?utm_source=site": "https://jobs.ashbyhq.com/benchling",
        "https://uscareers-medpace.icims.com/jobs/login?loginOnly=1":
            "https://uscareers-medpace.icims.com/jobs/search?ss=1",
        "https://www.smartrecruiters.com/web-sso/saml/HarvardUniversity/login":
            "https://jobs.smartrecruiters.com/HarvardUniversity",
        "https://careers.abbvie.com/en/job/manager-finance-in-irvine-ca-jid-33460": "https://careers.abbvie.com/en/jobs",
        "https://corteva.eightfold.ai/careers?elementor-preview=7199&ver=1&domain=corteva.com":
            "https://corteva.eightfold.ai/careers?domain=corteva.com",
    }
    for url, expected in cases.items():
        assert ats_board_url(url) == expected, (url, ats_board_url(url))


def test_board_company_name():
    assert board_company_name("https://careers-werfen.icims.com/jobs/search?ss=1") == "werfen"
    assert board_company_name("https://jobs.ashbyhq.com/benchling") == "benchling"
    assert board_company_name("https://pfizer.wd1.myworkdayjobs.com/PfizerCareers") == "pfizer"


HOMEPAGE = """
<a href="/about">About us</a>
<a href="/news/careers-day">Read about our careers day event and everything that happened on the day</a>
<a href="/careers">Careers</a>
<a href="https://www.linkedin.com/company/acme/jobs">Jobs on LinkedIn</a>
<a href="https://x.com/acme">Follow us on X</a>
<a href="/privacy">Privacy</a>
<a href="https://acme.wd1.myworkdayjobs.com/External/job/123">Search jobs</a>
"""


def test_extract_candidates():
    cands, n_links = extract_candidates(HOMEPAGE, "https://www.acme.com/", "acme.com")
    urls = [c.url for c in cands]
    assert n_links == 7
    assert urls[0] == "https://acme.wd1.myworkdayjobs.com/External/job/123"  # ATS link + "search jobs" wins
    assert "https://www.acme.com/careers" in urls
    assert not any("x.com" in u for u in urls)
    assert not any("linkedin" in u or "privacy" in u or u.endswith("/about") for u in urls)
    news = [c for c in cands if "news" in c.url]
    careers = [c for c in cands if c.url.endswith("/careers")][0]
    assert not news or news[0].score < careers.score  # long article teaser scores below the real link


def test_excluded_hosts():
    # bug found while writing these tests: "x.com" used to match any domain ending in "x.com"
    assert is_excluded_host("x.com") and is_excluded_host("www.linkedin.com") and is_excluded_host("www.indeed.com")
    assert not is_excluded_host("careers.idexx.com")
    assert not is_excluded_host("www.spacex.com")
    assert score_link("Careers", "https://careers.idexx.com/us/en", "idexx.com", CAREER_PHRASES) > 0


def test_analyze_page():
    html = ('<html><head><title>Careers at X</title></head><body>Search jobs. View all jobs. Apply now. '
            'apply apply apply <script type="application/ld+json">{"@type": "JobPosting", "title": "x"}</script>'
            '</body></html>')
    sig = analyze_page(Page(url="https://www.x.com/careers", status=200, html=html))
    assert sig.jobposting and "title" in sig.reasons and sig.score >= 12


def _response(status, headers=None, body=b"", url="https://www.x.com/"):
    r = requests.Response()
    r.status_code, r._content, r.url, r.encoding = status, body, url, "utf-8"
    r.headers = CaseInsensitiveDict(headers or {})
    return r


def test_describe_rejection():
    assert describe_rejection(_response(403, {"Server": "cloudflare"})) == "403 Cloudflare"
    assert describe_rejection(_response(403, {"Server": "AkamaiGHost"})) == "403 Akamai"
    assert describe_rejection(_response(403, {"Server": "CloudFront", "X-Amz-Cf-Id": "abc"})) == "403 CloudFront"
    assert describe_rejection(_response(202, {"x-amzn-waf-action": "challenge"})) == "202 AWS WAF challenge"
    assert describe_rejection(_response(404)) == ""                     # missing page is not a refusal
    assert describe_rejection(_response(503)) == ""                     # plain outage is not a refusal
    assert describe_rejection(_response(200)) == ""


# --------------------------------------------------------------------------- #
# ats_clients.py helpers
# --------------------------------------------------------------------------- #

def test_apply_location():
    j = Job(company="c", ats="a", job_id="1", title="t")
    apply_location(j, ["US, Boston MA", "Toronto, ON, Canada"])
    assert j.is_us is True and j.states == ["MA"] and j.city == "Boston"
    j = Job(company="c", ats="a", job_id="2", title="t")
    apply_location(j, ["Basel"], ["CH"])  # a structured non-US country decides
    assert j.is_us is False
    j = Job(company="c", ats="a", job_id="3", title="t")
    apply_location(j, ["North Chicago, IL"], ["US"], city="North Chicago, IL")
    assert j.city == "North Chicago" and j.states == ["IL"]


def test_jobposting_schema():
    html = """<script type="application/ld+json">{"@graph": [{"@type": "Organization"}, {"@type": "JobPosting",
      "description": "<p>Run assays.</p>", "employmentType": "FULL_TIME", "datePosted": "2026-11-01T00:00:00Z",
      "jobLocation": {"address": {"addressCountry": "US", "addressRegion": "MA", "addressLocality": "Boston"}},
      "baseSalary": {"currency": "USD", "value": {"minValue": 50000, "maxValue": 70000, "unitText": "YEAR"}}}]}
    </script>"""
    data = jobposting_ld(html)
    assert data and data["@type"] == "JobPosting"
    j = Job(company="c", ats="a", job_id="1", title="t")
    apply_jobposting(j, data)
    assert j.description == "Run assays." and j.posted == "2026-11-01"
    assert j.is_us and j.states == ["MA"] and j.city == "Boston"
    assert j.meta["salary"]["salary_annual_mid"] == 60000 and j.meta["salary"]["salary_source"] == "job board"


def test_structured_salary():
    s = structured_salary("20", "30", "usd", "per-hour-wage")
    assert (s["salary_period"], s["salary_currency"], s["salary_annual_mid"]) == ("hour", "USD", 52000)
    assert structured_salary(None, None, "USD", "year") == {}


# --------------------------------------------------------------------------- #
# full fetch_jobs.py pipeline against a fake job board
# --------------------------------------------------------------------------- #

class FakeBoard(ATSClient):
    """Stands in for a real job board: three openings, one only placeable after its detail page."""
    name = "FakeATS"

    def list_jobs(self, us_only=True):
        a = self._job("F1", "Scientist II", url="https://fake.example/F1", employment_type="Full time",
                      description="Basic Qualifications\nB.S. or M.S. in Biology with 3+ years of experience.\n"
                                  "The pay range is $90,000 - $110,000 per year. This is a hybrid role. "
                                  "Up to 10% travel.")
        apply_location(a, ["US, Boston MA"])
        b = self._job("F2", "Research Associate", url="https://fake.example/F2",
                      description="High school diploma required. $22 - $26 per hour. 2nd shift.")
        apply_location(b, ["Toronto, ON, Canada"])          # not US: dropped
        c = self._job("F3", "Director, Biology", url="https://fake.example/F3")
        apply_location(c, ["Remote"])                        # unknown until the detail page
        self.total_openings = 3
        return [a, b, c]

    def enrich(self, job):
        job.description = "PhD in Biology required. Relocation assistance is available."
        apply_location(job, ["Cambridge, Massachusetts, United States"])


def test_full_pipeline_offline():
    CLIENTS["FakeATS"] = FakeBoard
    try:
        row = {"input_url": "fakeco.com", "company": "FakeCo", "ats": "FakeATS", "status": "listings_found",
               "listings_url": "https://fake.example/board", "careers_url": "", "rejected_requests": "",
               "manual_check": "", "notes": ""}
        args = argparse.Namespace(all_locations=False, max_jobs=0, detail_workers=2, delay=0, timeout=5,
                                  ignore_robots=False)
        rep = process_company(row, args, keyword_res(["Biology"]))
    finally:
        del CLIENTS["FakeATS"]

    assert rep.status == "ok" and rep.total_openings == 3 and rep.us_openings == 2 and rep.location_unknown == 0
    jobs = {j.job_id: j for j in rep.jobs}
    assert set(jobs) == {"F1", "F3"}
    f1, f3 = jobs["F1"], jobs["F3"]
    assert (f1.career_level, f1.min_education, f1.min_years_experience) == ("mid", "Bachelor's", 3)
    assert (f1.salary_min, f1.salary_max, f1.salary_period) == (90000, 110000, "year")
    assert (f1.work_arrangement, f1.travel, f1.states, f1.city) == ("hybrid", "up to 10%", ["MA"], "Boston")
    assert (f3.career_level, f3.min_education, f3.states, f3.relocation) == ("senior", "PhD", ["MA"], "offered")
    assert f1.keywords == ["Biology"] and f3.keywords == ["Biology"]

    with tempfile.TemporaryDirectory() as d:
        write_outputs([rep], ["Biology"], d)
        assert sorted(os.listdir(d)) == ["jobs.csv", "jobs.jsonl", "summary.csv", "us_locations.csv"]
        with open(os.path.join(d, "jobs.csv"), encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 2 and {"salary_annual_mid", "work_arrangement", "certifications"} <= set(rows[0])
        with open(os.path.join(d, "summary.csv"), encoding="utf-8") as f:
            summary = {r["company"]: r for r in csv.DictReader(f)}
        assert summary["FakeCo"]["us_openings"] == "2" and summary["FakeCo"]["pay_disclosed"] == "1"
        assert summary["FakeCo"]["kw_Biology"] == "2" and "ALL" in summary

        path = os.path.join(d, "jobs.db")
        conn = database.open_db(path)
        run_id = database.start_run(conn, "pipeline-test", {}, complete=True, us_only=True)
        stats = database.save_run(conn, run_id, [rep], complete=True, us_only=True)
        conn.close()
        assert (stats["new"], stats["open_jobs"]) == (2, 2)
        conn = sqlite3.connect(path)
        assert conn.execute("SELECT salary_annual_mid FROM jobs WHERE job_id = 'F1'").fetchone() == (100000.0,)
        assert conn.execute("SELECT COUNT(*) FROM job_keywords WHERE keyword = 'Biology'").fetchone() == (2,)
        conn.close()


# --------------------------------------------------------------------------- #
# edge cases: company identity, cut-short listings, capped runs, shared robots.txt, Workday states
# --------------------------------------------------------------------------- #

def test_company_identity():
    from fetch_jobs import company_identity
    # a pasted job board is keyed by the board, never by the ATS's domain
    lilly = company_identity("https://lilly.wd115.myworkdayjobs.com/lly")
    abbott = company_identity("https://abbott.wd5.myworkdayjobs.com/en-US/abbottcareers/job/123")
    assert lilly == ("board:lilly.wd115.myworkdayjobs.com/lly", "")
    assert abbott == ("board:abbott.wd5.myworkdayjobs.com/abbottcareers", "")
    assert company_identity("https://jobs.ashbyhq.com/benchling")[0] != company_identity("https://jobs.ashbyhq.com/other")[0]
    for url in ("https://boards.greenhouse.io/foo", "jobs.lever.co/bar", "https://jobs.ashbyhq.com/baz"):
        key, _ = company_identity(url)
        assert key.startswith("board:") and key not in ("greenhouse.io", "lever.co", "ashbyhq.com"), (url, key)
    # homepages and careers pages: the registered domain, as before (keeps existing databases' keys)
    assert company_identity("pfizer.com") == ("pfizer.com", "pfizer.com")
    assert company_identity("https://careers.astrazeneca.com/") == ("astrazeneca.com", "astrazeneca.com")
    assert company_identity("https://www.idexx.com/en/about-idexx/careers/") == ("idexx.com", "idexx.com")
    assert company_identity("") == ("", "")


class ShortBoard(FakeBoard):
    """Says it has 100 jobs but the listing stops after 3 (an empty page mid-way)."""
    name = "ShortATS"

    def list_jobs(self, us_only=True):
        jobs = super().list_jobs(us_only)
        self.total_openings = self.expected_listed = 100
        return jobs


class EmptyBoard(FakeBoard):
    name = "EmptyATS"

    def list_jobs(self, us_only=True):
        self.total_openings = 0
        return []


class ManyBoard(FakeBoard):
    """Ten US jobs with known locations, two non-US and one unknown, for --max-jobs runs."""
    name = "ManyATS"

    def list_jobs(self, us_only=True):
        jobs = []
        for i in range(10):
            j = self._job(f"M{i}", "Scientist I", url=f"https://fake.example/M{i}", description="BS required.")
            apply_location(j, ["Boston, MA"])
            jobs.append(j)
        for i, loc in enumerate(["Basel, Switzerland", "Toronto, ON"]):
            j = self._job(f"X{i}", "Scientist I", url=f"https://fake.example/X{i}")
            apply_location(j, [loc])
            jobs.append(j)
        u = self._job("U1", "Scientist I", url="https://fake.example/U1", description="x")
        apply_location(u, ["3 Locations"])
        jobs.append(u)
        self.total_openings = len(jobs)
        return jobs

    def enrich(self, job):
        pass


def _run_fake(board_cls, **overrides):
    CLIENTS[board_cls.name] = board_cls
    try:
        row = {"input_url": "fakeco.com", "company": "FakeCo", "ats": board_cls.name, "status": "listings_found",
               "listings_url": "https://fake.example/board", "careers_url": "", "rejected_requests": "",
               "manual_check": "", "notes": ""}
        args = argparse.Namespace(**{"all_locations": False, "max_jobs": 0, "detail_workers": 2, "delay": 0,
                                     "timeout": 5, "ignore_robots": True, **overrides})
        return process_company(row, args, {})
    finally:
        del CLIENTS[board_cls.name]


def test_listing_completeness():
    assert _run_fake(FakeBoard).listing_complete          # no board total: trusted
    short = _run_fake(ShortBoard)
    assert short.status == "ok" and not short.listing_complete
    assert any("cut short (3 of 100" in n for n in short.notes)
    empty = _run_fake(EmptyBoard)
    assert empty.status == "ok" and not empty.listing_complete and empty.jobs == []
    # the 95% tolerance: a job or two closing mid-run doesn't stop closures
    c = ATSClient("https://x.example", "X", requests.Session())
    c.expected_listed = 100
    assert c.listing_complete(95) and c.listing_complete(100) and not c.listing_complete(94)
    c.expected_listed = None
    assert c.listing_complete(0)

    # a cut-short or empty listing must not close the jobs it missed; a complete one still does
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "jobs.db")
        expected = [(_run_fake(ManyBoard), 0, 10),   # 10 US jobs M0..M9
                    (short, 0, 12),                  # F1, F3 added; the M jobs it missed stay open
                    (empty, 0, 12),                  # nothing listed: nothing closed
                    # complete listing: everything it doesn't list closes - the 10 M jobs and the 2
                    # ShortATS ones (the ATS is part of a job's id, so FakeATS's F1/F3 are new jobs)
                    (_run_fake(FakeBoard), 12, 2)]
        for rep, closed, still_open in expected:
            conn = database.open_db(path)
            s = database.save_run(conn, database.start_run(conn, "t", {}, True, True), [rep], True, True)
            conn.close()
            assert (s["closed"], s["open_jobs"]) == (closed, still_open), (rep.ats, s)


class RobotsBoard(FakeBoard):
    name = "RobotsATS"

    def list_jobs(self, us_only=True):
        from ats_clients import RobotsDisallowed
        raise RobotsDisallowed("robots.txt on fake.example disallows /api/jobs")


def test_robots_disallowed_board_is_skipped_with_message():
    rep = _run_fake(RobotsBoard, ignore_robots=False)
    assert rep.status == "robots_disallowed" and rep.jobs == [] and rep.total_openings is None
    assert rep.manual_check.startswith("skipped: robots.txt on fake.example")
    assert "try again with that careers search URL" in rep.manual_check
    assert any("try again with the company's careers search URL" in n for n in rep.notes)


def test_capped_run_counts_whole_listing():
    capped = _run_fake(ManyBoard, max_jobs=3)
    assert len(capped.jobs) == 3                               # only the sample is classified and saved
    assert capped.us_openings == 10 and capped.location_unknown == 1  # counts still cover the listing
    full = _run_fake(ManyBoard)
    assert (full.us_openings, full.location_unknown, len(full.jobs)) == (10, 1, 10)
    assert _run_fake(ManyBoard, max_jobs=100).us_openings == 10   # a cap above the listing changes nothing


class _CountingSession(requests.Session):
    def __init__(self):
        super().__init__()
        self.robots_requests = 0

    def get(self, url, **kw):
        self.robots_requests += url.endswith("/robots.txt")
        return _response(200, body=b"User-agent: *\nDisallow: /private\n", url=url)


def test_robots_cache_shared_across_companies():
    import ats_clients
    ats_clients._ROBOTS_CACHE.pop("https://robots-test.example", None)
    sessions = [_CountingSession() for _ in range(3)]
    for s in sessions:  # three companies on the same job-board host
        c = ATSClient("https://robots-test.example/board", "X", s, respect_robots=True)
        c._check_robots("https://robots-test.example/api/jobs")
        try:
            c._check_robots("https://robots-test.example/private/x")
            raise AssertionError("disallowed path was allowed")
        except ats_clients.RobotsDisallowed:
            pass
    assert sum(s.robots_requests for s in sessions) == 1
    ats_clients._ROBOTS_CACHE.pop("https://robots-test.example", None)


def test_workday_state_lookup_stops_early():
    from ats_clients import WorkdayClient
    wd = WorkdayClient("https://acme.wd1.myworkdayjobs.com/External", "Acme", requests.Session())
    wd.us_filtered, wd.facets = True, {"country": ["us"]}
    wd.facet_data = {"facets": [{"facetParameter": "locationRegion", "values": [
        {"descriptor": "California", "id": "ca", "count": 300},
        {"descriptor": "Nebraska", "id": "ne", "count": 2},
        {"descriptor": "Texas", "id": "tx", "count": 40},
        {"descriptor": "Ontario", "id": "on", "count": 5},     # not a US state: skipped
    ]}]}
    calls = []
    by_state = {"ne": [{"externalPath": "/job/a"}], "tx": [{"externalPath": "/job/b"}],
                "ca": [{"externalPath": f"/job/c{i}"} for i in range(300)]}

    def fake_all_postings(facets, first=None):
        state = facets["locationRegion"][0]
        calls.append(state)
        return by_state.get(state, [])
    wd._all_postings = fake_all_postings
    a, b, known = (Job("Acme", "Workday", x, "t", is_us=True, meta={"path": f"/job/{x}"}) for x in ("a", "b", "c0"))
    known.states = ["CA"]
    wd.finalize([a, b, known])
    assert a.states == ["NE"] and b.states == ["TX"] and known.states == ["CA"]
    assert calls == ["ne", "tx"]  # smallest first, stopped before the 300-job state; Ontario never asked


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("all pipeline checks passed")
