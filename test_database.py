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


if __name__ == "__main__":
    test_create_then_add()
    test_failed_company_closes_nothing()
    test_schema_upgrade()
    print("all database checks passed")
