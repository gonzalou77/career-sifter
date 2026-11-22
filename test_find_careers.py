"""Quick checks for find_careers.py input handling. Run: python test_find_careers.py"""
import os
import tempfile

from datetime import date, timedelta

from find_careers import (Result, checked_recently, classify_input, load_known, load_sites, manual_check_note,
                          needs_manual, save_known, site_key)

INPUTS = [
    ("pfizer.com", "homepage"),
    ("https://www.pfizer.com/", "homepage"),
    ("https://www.pfizer.com/about/leadership", "homepage"),
    ("https://careers.astrazeneca.com", "careers_page"),
    ("https://jobs.berkeley.edu/", "careers_page"),
    ("https://www.medpace.com/careers/", "careers_page"),
    ("https://hr.mit.edu/jobs/search", "careers_page"),
    ("https://www.example.com/en/about-us/join-us", "careers_page"),
    ("https://careers.gene.com/us/en", "careers_page"),
    ("https://www.example.com/us/en/search-results", "careers_page"),
    ("https://pfizer.wd1.myworkdayjobs.com/PfizerCareers", "job_board"),
    ("https://jobs.ashbyhq.com/benchling", "job_board"),
    ("https://careers-werfen.icims.com/jobs/search?ss=1", "job_board"),
]


def test_classify_input():
    for url, expected in INPUTS:
        assert classify_input(url) == expected, (url, classify_input(url))


def test_load_sites_csv_careers_column():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "sites.csv")
        with open(path, "w", encoding="utf-8") as f:
            f.write("company,url,careers_url\n"
                    "AbbVie,abbvie.com,https://careers.abbvie.com\n"
                    "Pfizer,pfizer.com,\n"
                    "AstraZeneca,,https://careers.astrazeneca.com\n")
        assert load_sites(path) == [
            ("abbvie.com", "AbbVie", "https://careers.abbvie.com"),
            ("pfizer.com", "Pfizer", ""),
            ("https://careers.astrazeneca.com", "AstraZeneca", "https://careers.astrazeneca.com"),
        ]


def test_site_key():
    # homepage, www and careers subdomain all map to one saved entry
    assert site_key("abbvie.com") == site_key("https://www.abbvie.com/") == site_key("https://careers.abbvie.com")
    # a pasted board is keyed by the board, not by myworkdayjobs.com
    assert site_key("https://pfizer.wd1.myworkdayjobs.com/PfizerCareers/job/x") == \
        "board:pfizer.wd1.myworkdayjobs.com/PfizerCareers"
    assert site_key("https://regeneron.wd1.myworkdayjobs.com/Careers") != site_key("https://pfizer.wd1.myworkdayjobs.com/x")


def test_known_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "known.csv")
        assert load_known(path) == {}
        entry = {"key": "abbvie.com", "company": "AbbVie", "input_url": "abbvie.com", "status": "listings_found",
                 "careers_url": "https://careers.abbvie.com/", "listings_url": "https://careers.abbvie.com/en/jobs",
                 "ats": "Attrax", "method": "careers-input", "last_verified": "2026-11-22",
                 "last_checked": "2026-11-22", "manual_check": ""}
        save_known(path, {"abbvie.com": entry})
        assert load_known(path) == {"abbvie.com": entry}


def test_manual_check():
    # no job board on the careers page: needs a manual look, until a board is pasted in
    assert needs_manual({"status": "careers_page_found", "careers_url": "https://x.com/careers", "listings_url": ""})
    assert needs_manual({"status": "blocked", "listings_url": ""})
    assert not needs_manual({"status": "careers_page_found", "listings_url": "https://jobs.lever.co/x"})
    assert not needs_manual({"status": "listings_found", "listings_url": "https://x.wd1.myworkdayjobs.com/X"})
    # entries saved before 'status' existed
    assert not needs_manual({"listings_url": "https://jobs.ashbyhq.com/x"})
    assert needs_manual({"careers_url": "https://x.com/careers", "listings_url": ""})

    today = date.today()
    assert checked_recently({"last_checked": (today - timedelta(days=3)).isoformat()}, 30)
    assert not checked_recently({"last_checked": (today - timedelta(days=31)).isoformat()}, 30)
    assert not checked_recently({}, 30)

    r = Result(input_url="schrodinger.com", status="careers_page_found",
               careers_url="https://www.schrodinger.com/company/careers/")
    assert "open https://www.schrodinger.com/company/careers/ in a browser" in manual_check_note(r)
    assert manual_check_note(Result(input_url="pfizer.com", status="listings_found", ats="Workday")) == ""
    assert "refuses scripts" in manual_check_note(Result(input_url="lilly.com", status="blocked"))


# --------------------------------------------------------------------------- #
# robots.txt and unreachable careers pages: skipped, with a message saying what to paste in
# --------------------------------------------------------------------------- #

def _scanner(input_url, careers_url="", status=404, robots_allows=True):
    """A SiteScanner with the network stubbed out: every page answers `status`, nothing is guessed."""
    import argparse
    from find_careers import Page, SiteScanner
    args = argparse.Namespace(ignore_robots=False, timeout=5, delay=0, render=False, verify=3)
    s = SiteScanner(input_url, "", args, careers_url)
    s.fetched = []
    s.allowed = lambda url: robots_allows
    s.probe_ats_boards = lambda: None  # guessed job-board addresses would need the network
    real_get = s.get

    def fake_get(url, record_rejection=True):
        if not s.allowed(url):
            return real_get(url, record_rejection)  # records the robots.txt skip; no request is made
        return None  # e.g. sitemaps: unreachable

    def fake_fetch(url):
        s.fetched.append(url)
        if not s.allowed(url):
            return s.get(url)
        return Page(url=url, status=status, html="<html><title>Example</title><a href='/about'>About</a></html>"
                    if status == 200 else "")
    s.get, s.fetch = fake_get, fake_fetch
    return s


def test_robots_and_unreachable_messages():
    from find_careers import careers_failed_note, robots_note
    msg = robots_note("https://corteva.eightfold.ai/careers?domain=corteva.com")
    assert msg.startswith("skipped: robots.txt on corteva.eightfold.ai") and "careers search URL" in msg
    msg = careers_failed_note("https://careers.example.com/")
    assert "please manually check for the careers page URL or job search URL and try again" in msg
    assert "careers_url" in msg and "listings_url" in msg  # says where to paste it


def test_careers_url_that_fails_does_not_use_homepage():
    s = _scanner("example.com", careers_url="https://careers.example.com/jobs-here")
    r = s.scan()
    assert r.status == "error" and r.input_type == "careers_page"
    assert "please manually check for the careers page URL or job search URL" in r.manual_check
    assert "https://careers.example.com/jobs-here" in r.manual_check
    # only the careers host was tried (its page, then its usual search pages); never the homepage
    assert s.fetched[0] == "https://careers.example.com/jobs-here"
    assert all(u.startswith("https://careers.example.com/") for u in s.fetched), s.fetched
    assert "homepage is not tried" in r.notes

    # a careers page pasted as the input itself behaves the same way
    r = _scanner("https://www.example.com/careers").scan()
    assert r.status == "error" and "please manually check" in r.manual_check


def test_robots_disallowed_site_is_skipped_with_message():
    s = _scanner("https://careers.example.com/", robots_allows=False)
    r = s.scan()
    assert r.status == "error"
    assert r.manual_check.startswith("skipped: robots.txt on careers.example.com")
    assert "careers search URL" in r.manual_check
    assert "robots.txt disallows" in r.notes
    # a homepage blocked by robots.txt gets the robots message, not "could not load the site"
    r = _scanner("example.com", robots_allows=False).scan()
    assert r.manual_check.startswith("skipped: robots.txt on")
    # a reachable site that just has no careers page keeps the search-for-it note
    r = _scanner("example.com", status=200).scan()
    assert r.status == "not_found" and "search 'example.com careers'" in r.manual_check


def test_failed_board_is_not_guessed_from_ats_domain():
    """A pasted job board that answers 404 must not guess boards from the ATS's own domain
    (boards.greenhouse.io -> slug "greenhouse" would find Greenhouse's own job board)."""
    import argparse
    from find_careers import Page, SiteScanner
    args = argparse.Namespace(ignore_robots=True, timeout=5, delay=0, render=False, verify=3)
    s = SiteScanner("https://boards.greenhouse.io/deadco", "", args)
    s.fetch = lambda url: Page(url=url, status=404)
    guessed = []
    s.probe_ats_boards = lambda: guessed.append(s.base_domain)
    r = s.scan()
    assert r.status == "not_found" and r.input_type == "job_board" and guessed == [], (r.status, guessed)


def test_refused_careers_page_is_not_blamed_on_robots():
    """The careers page was refused (403); a search page tried afterwards was robots-disallowed.
    The message should be about the careers page, not robots.txt."""
    from find_careers import Page
    s = _scanner("example.com", careers_url="https://careers.example.com/")
    s.allowed = lambda url: "/search/" not in url

    def fetch(url):  # careers page refused, its search pages missing, /search/?q= robots-disallowed
        if not s.allowed(url):
            return s.get(url)
        return Page(url=url, status=403 if url == "https://careers.example.com/" else 404)
    s.fetch = fetch
    r = s.scan()
    assert r.status == "error" and "robots.txt disallows" in r.notes
    assert r.manual_check.startswith("the careers page https://careers.example.com/ could not be loaded"), \
        r.manual_check
    # the careers page itself disallowed: robots.txt is the reason
    r = _scanner("https://careers.example.com/", robots_allows=False).scan()
    assert r.manual_check.startswith("skipped: robots.txt on careers.example.com")


if __name__ == "__main__":
    test_failed_board_is_not_guessed_from_ats_domain()
    test_refused_careers_page_is_not_blamed_on_robots()
    test_classify_input()
    test_load_sites_csv_careers_column()
    test_site_key()
    test_known_roundtrip()
    test_manual_check()
    test_robots_and_unreachable_messages()
    test_careers_url_that_fails_does_not_use_homepage()
    test_robots_disallowed_site_is_skipped_with_message()
    print("all find_careers checks passed")
