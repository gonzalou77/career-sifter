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


def board_report(key, company, jobs, status="ok", complete=True):
    return CompanyReport(company=company, ats="Workday", board_url="https://" + key.split(":", 1)[-1],
                         status=status, total_openings=len(jobs), us_openings=len(jobs), jobs=jobs, company_key=key,
                         listing_complete=complete)


def test_pasted_boards_stay_separate():
    """Two pasted Workday boards: separate companies, and the same requisition id at both is two jobs.
    One of them blocked on the next run must not have its jobs closed by the other."""
    lilly, abbott = "board:lilly.wd115.myworkdayjobs.com/lly", "board:abbott.wd5.myworkdayjobs.com/abbottcareers"
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "jobs.db")
        s = run(path, [board_report(lilly, "lilly", [job("R1", "Scientist")]),
                       board_report(abbott, "abbott", [job("R1", "Chemist"), job("R2", "Engineer")])])
        assert (s["new"], s["companies"], s["open_jobs"]) == (3, 2, 3)
        s = run(path, [board_report(lilly, "lilly", [job("R1", "Scientist")]),
                       board_report(abbott, "abbott", [], status="blocked")])
        assert s["closed"] == 0 and s["open_jobs"] == 3
        conn = sqlite3.connect(path)
        assert conn.execute("SELECT COUNT(*) FROM companies").fetchone() == (2,)
        assert conn.execute("SELECT COUNT(DISTINCT company_key) FROM jobs WHERE job_id = 'R1'").fetchone() == (2,)
        assert conn.execute("SELECT title FROM jobs WHERE company_key = ? AND job_id = 'R1'", (abbott,)).fetchone() \
            == ("Chemist",)
        conn.close()


def test_same_company_twice_in_one_run():
    """pfizer.com and careers.pfizer.com in one input share a key: one blocked row stops closures,
    and the run's snapshot adds both rows' counts instead of keeping only the last."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "jobs.db")
        run(path, [report([job("R1", "Scientist"), job("R2", "Engineer")])])
        s = run(path, [report([job("R1", "Scientist")]), report([], status="blocked")])
        assert s["closed"] == 0 and s["open_jobs"] == 2   # R2 may be listed on the blocked board
        s = run(path, [report([job("R1", "Scientist")]), report([job("R3", "Analyst")])])
        assert s["closed"] == 1 and s["open_jobs"] == 2   # both rows collected: R2 really closed
        conn = sqlite3.connect(path)
        snap = conn.execute("SELECT status, us_openings, jobs_saved FROM company_snapshots "
                            "WHERE run_id = 2 AND company_key = 'lilly.com'").fetchone()
        assert snap == ("ok+blocked", 1, 1)
        assert conn.execute("SELECT us_openings FROM company_snapshots WHERE run_id = 3").fetchone() == (2,)
        conn.close()


def test_incomplete_listing_closes_nothing():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "jobs.db")
        run(path, [report([job("R1", "Scientist"), job("R2", "Engineer")])])
        rep = report([job("R1", "Scientist")])
        rep.listing_complete = False
        s = run(path, [rep])
        assert s["closed"] == 0 and s["open_jobs"] == 2


def test_duplicate_job_in_one_listing():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "jobs.db")
        s = run(path, [report([job("R1", "Scientist"), job("R1", "Scientist", ("IN", "OH"))])])
        assert (s["new"], s["updated"], s["total_jobs"]) == (1, 1, 1)
        conn = sqlite3.connect(path)
        assert sorted(conn.execute("SELECT state FROM job_states")) == [("IN",), ("OH",)]  # last copy wins
        conn.close()


def test_us_only_run_leaves_non_us_jobs():
    """A US-only run can't see non-US jobs, so it must not close those saved by an --all-locations run."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "jobs.db")
        basel = job("X1", "Chemist", states=())
        basel.is_us = False
        conn = database.open_db(path)
        database.save_run(conn, database.start_run(conn, "t", {}, True, False), [report([job("R1", "S"), basel])],
                          complete=True, us_only=False)
        s = database.save_run(conn, database.start_run(conn, "t", {}, True, True), [report([job("R1", "S")])],
                              complete=True, us_only=True)
        conn.close()
        assert s["closed"] == 0 and s["open_jobs"] == 2


def test_reopened_job():
    """A job that disappears and comes back is open again, with its first-seen run kept."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "jobs.db")
        run(path, [report([job("R1", "Scientist"), job("R2", "Engineer")])])
        run(path, [report([job("R1", "Scientist")])])
        s = run(path, [report([job("R1", "Scientist"), job("R2", "Engineer")])])
        assert s["closed"] == 0 and s["open_jobs"] == 2
        conn = sqlite3.connect(path)
        assert conn.execute("SELECT is_open, closed_at, first_seen_run, last_seen_run FROM jobs "
                            "WHERE job_id = 'R2'").fetchone() == (1, None, 1, 3)
        conn.close()


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
    pfizer = [fixture_job("Pfizer", "P2", "Vice President, Research", "executive", ["NY"], ["PhD"], 300000, 400000)]
    if run == 1:  # run 2's listing is cut short and misses P1: it must stay open
        pfizer.insert(0, fixture_job("Pfizer", "P1", "Principal Scientist", "senior", ["NY"], ["PhD"]))
    # a job board pasted as the input: keyed by the board, and its job id R1 is the same as Lilly's.
    # Blocked on run 2: its job stays open.
    acme = [fixture_job("Acme", "R1", "Lab Technician", "early", ["OH"], ["Bachelor's"])] if run == 1 else []
    return [CompanyReport(company="Lilly", ats="Workday", board_url="https://lilly.example/board", status="ok",
                          total_openings=640 + run, us_openings=len(lilly), jobs=lilly, company_key="lilly.com"),
            CompanyReport(company="Pfizer", ats="Workday", board_url="https://pfizer.example/board", status="ok",
                          total_openings=700, us_openings=len(pfizer), jobs=pfizer, company_key="pfizer.com",
                          listing_complete=run == 1),
            CompanyReport(company="acme", ats="Workday", board_url="https://acme.wd1.myworkdayjobs.com/External",
                          status="ok" if run == 1 else "blocked", total_openings=1 if run == 1 else None,
                          us_openings=len(acme), jobs=acme, company_key=ACME_KEY)]


ACME_KEY = "board:acme.wd1.myworkdayjobs.com/external"


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
    assert conn.execute("SELECT COUNT(*), SUM(is_open) FROM jobs").fetchone() == (7, 6)
    assert conn.execute("SELECT is_open FROM jobs WHERE job_id = 'R3'").fetchone() == (0,)
    assert conn.execute("SELECT first_seen_run FROM jobs WHERE job_id = 'R4'").fetchone() == (2,)
    assert conn.execute("SELECT is_open, last_seen_run FROM jobs WHERE job_id = 'P1'").fetchone() == (1, 1)
    assert conn.execute("SELECT is_open FROM jobs WHERE company_key = ?", (ACME_KEY,)).fetchone() == (1,)
    assert conn.execute("SELECT COUNT(*) FROM companies").fetchone() == (3,)
    conn.close()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("all database checks passed")
