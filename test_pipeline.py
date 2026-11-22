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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("all pipeline checks passed")
