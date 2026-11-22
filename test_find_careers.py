"""Quick checks for find_careers.py input handling. Run: python test_find_careers.py"""
import os
import tempfile

from find_careers import classify_input, load_known, load_sites, save_known, site_key

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
        entry = {"key": "abbvie.com", "company": "AbbVie", "input_url": "abbvie.com",
                 "careers_url": "https://careers.abbvie.com/", "listings_url": "https://careers.abbvie.com/en/jobs",
                 "ats": "Attrax", "method": "careers-input", "last_verified": "2026-11-22"}
        save_known(path, {"abbvie.com": entry})
        assert load_known(path) == {"abbvie.com": entry}


if __name__ == "__main__":
    test_classify_input()
    test_load_sites_csv_careers_column()
    test_site_key()
    test_known_roundtrip()
    print("all find_careers checks passed")
