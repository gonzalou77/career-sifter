"""Quick checks for database.py. Run: python test_database.py"""
import os
import sqlite3
import tempfile

import database
from ats_clients import Job
from fetch_jobs import CompanyReport


def job(job_id, title, states=("IN",), salary=None):
    j = Job(company="Lilly", ats="Workday", job_id=job_id, title=title, is_us=True, states=list(states),
            certifications=["Six Sigma"], education_mentioned=["Bachelor's", "PhD"])
    j.salary_annual_mid = salary
    return j


def report(jobs, status="ok"):
    return CompanyReport(company="Lilly", ats="Workday", board_url="https://lilly.wd115.myworkdayjobs.com/lly",
                         status=status, total_openings=647, us_openings=len(jobs), jobs=jobs, company_key="lilly.com")


def run(path, reports, complete=True):
    conn = database.open_db(path)
    run_id = database.start_run(conn, "test.csv", {}, complete, us_only=True)
    stats = database.save_run(conn, run_id, reports, complete, us_only=True)
    conn.close()
    return stats


def test_create_then_add():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "jobs.db")
        assert not os.path.exists(path)

        s = run(path, [report([job("R1", "Scientist I", salary=90000), job("R2", "Engineer II", ("IN", "NJ"))])])
        assert os.path.exists(path)  # created on first run
        assert (s["new"], s["updated"], s["closed"], s["open_jobs"]) == (2, 0, 0, 2)

        # second complete run: R1 still listed (title changed), R2 gone, R3 new
        s = run(path, [report([job("R1", "Scientist II", salary=95000), job("R3", "Director")])])
        assert (s["new"], s["updated"], s["closed"], s["total_jobs"], s["open_jobs"]) == (1, 1, 1, 3, 2)

        # a capped run never closes anything
        s = run(path, [report([job("R3", "Director")])], complete=False)
        assert s["closed"] == 0 and s["open_jobs"] == 2

        conn = sqlite3.connect(path)
        r1 = conn.execute("SELECT title, salary_annual_mid, first_seen_run, last_seen_run, is_open FROM jobs "
                          "WHERE job_id = 'R1'").fetchone()
        assert r1 == ("Scientist II", 95000.0, 1, 2, 1)  # updated, first_seen kept
        assert conn.execute("SELECT is_open, closed_at IS NOT NULL FROM jobs WHERE job_id = 'R2'").fetchone() == (0, 1)
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM company_snapshots WHERE company_key = 'lilly.com'").fetchone()[0] == 3
        assert sorted(conn.execute("SELECT state FROM job_states WHERE job_uid LIKE '%R2'")) == [("IN",), ("NJ",)]
        assert conn.execute("SELECT COUNT(*) FROM v_open_jobs").fetchone()[0] == 2
        conn.close()


def test_failed_company_closes_nothing():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "jobs.db")
        run(path, [report([job("R1", "Scientist I")])])
        s = run(path, [report([], status="blocked")])  # blocked this time: its jobs are not closed
        assert s["closed"] == 0 and s["open_jobs"] == 1


def test_schema_upgrade():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "old.db")
        conn = sqlite3.connect(path)  # a database from before 'certifications' existed
        conn.execute("CREATE TABLE jobs (job_uid TEXT PRIMARY KEY, company_key TEXT, title TEXT)")
        conn.commit()
        conn.close()
        conn = database.open_db(path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        conn.close()
        assert {"certifications", "salary_annual_mid", "work_arrangement", "is_open", "first_seen_run"} <= cols


# --------------------------------------------------------------------------- #
# Known-contents database for the R tests (tests/testthat/test-database.R).
# Two complete runs; the R tests check exact answers against these values.
# --------------------------------------------------------------------------- #

FIXTURE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests", "fixture.db")


def fixture_job(company, job_id, title, level, states, education, low=None, high=None, period="year",
                url=True):
    j = Job(company=company, ats="Workday", job_id=job_id, title=title, is_us=True, states=list(states),
            career_level=level, education_mentioned=list(education),
            min_education=education[0] if education else "Not stated",
            url=f"https://example.com/{company.lower()}/{job_id}" if url else "")
    if low is not None:
        per_year = 2080 if period == "hour" else 1
        j.salary_min, j.salary_max, j.salary_currency, j.salary_period = low, high, "USD", period
        j.salary_annual_mid, j.salary_source = (low + high) / 2 * per_year, "description"
    return j


def fixture_reports(run):
    lilly = [fixture_job("Lilly", "R1", "Scientist I" if run == 1 else "Scientist II", "early" if run == 1 else "mid",
                         ["IN"], ["Bachelor's"], 80000 if run == 1 else 85000, 100000 if run == 1 else 105000),
             fixture_job("Lilly", "R2", "Associate Director", "senior", ["IN", "NJ"], ["PhD"], 150000, 200000)]
    if run == 1:
        lilly.append(fixture_job("Lilly", "R3", "Data Analyst Intern", "early", ["CA"], ["Bachelor's"],
                                 25, 30, "hour"))   # gone in run 2 -> closed
    else:
        lilly.append(fixture_job("Lilly", "R4", "Senior Engineer", "senior", ["TX"], ["Bachelor's"],
                                 120000, 140000))   # new in run 2
    pfizer = [fixture_job("Pfizer", "P1", "Principal Scientist", "senior", ["NY"], ["PhD"]),
              fixture_job("Pfizer", "P2", "Vice President, Research", "executive", ["NY"], ["PhD"], 300000, 400000)]
    return [CompanyReport(company="Lilly", ats="Workday", board_url="https://lilly.example/board", status="ok",
                          total_openings=640 + run, us_openings=len(lilly), jobs=lilly, company_key="lilly.com"),
            CompanyReport(company="Pfizer", ats="Workday", board_url="https://pfizer.example/board", status="ok",
                          total_openings=700, us_openings=len(pfizer), jobs=pfizer, company_key="pfizer.com")]


def build_fixture(path=FIXTURE_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        os.remove(path)
    for run_no in (1, 2):
        conn = database.open_db(path)
        run_id = database.start_run(conn, "fixture", {}, complete=True, us_only=True)
        database.save_run(conn, run_id, fixture_reports(run_no), complete=True, us_only=True)
        conn.close()
    return path


def test_build_fixture():
    """Builds tests/fixture.db for the R tests and checks the values they rely on."""
    path = build_fixture()
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*), SUM(is_open) FROM jobs").fetchone() == (6, 5)
    assert conn.execute("SELECT is_open FROM jobs WHERE job_id = 'R3'").fetchone() == (0,)
    assert conn.execute("SELECT first_seen_run FROM jobs WHERE job_id = 'R4'").fetchone() == (2,)
    conn.close()


if __name__ == "__main__":
    test_create_then_add()
    test_failed_company_closes_nothing()
    test_schema_upgrade()
    test_build_fixture()
    print("all database checks passed")
