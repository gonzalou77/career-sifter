"""
database.py - keep collected jobs in a SQLite database (default career_sifter.db).

The database is created on first use; every later run adds to it.

  runs               one row per fetch_jobs.py run (when, input, settings, counts)
  companies          one row per company: name, job board, ATS, first/last seen
  company_snapshots  one row per company per run: status, total and US openings -> openings over time
  jobs               one row per job, keyed by company + ATS + the board's own job id. Updated each run
                     that sees it (first_seen_* / last_seen_*). A complete run that no longer lists it
                     sets is_open = 0 and closed_at.
  job_states, job_education, job_certifications, job_keywords
                     one row per value of the multi-value fields, for GROUP BY / COUNT
  v_open_jobs, v_latest_snapshots   convenience views

Columns added to Job later are added to an existing database automatically.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import fields
from datetime import datetime, timezone

from ats_clients import Job

FLOAT_COLUMNS = {"salary_min", "salary_max", "salary_annual_mid"}
INT_COLUMNS = {"min_years_experience", "contract_months", "is_us", "remote"}
LIST_COLUMNS = {"locations", "states", "education_mentioned", "keywords", "certifications"}
JOB_COLUMNS = [f.name for f in fields(Job) if f.name != "meta"]
CHILD_TABLES = {  # table -> (value column, Job list field)
    "job_states": ("state", "states"),
    "job_education": ("level", "education_mentioned"),
    "job_certifications": ("certification", "certifications"),
    "job_keywords": ("keyword", "keywords"),
}
TRACKING_COLUMNS = ["first_seen_run INTEGER", "last_seen_run INTEGER", "first_seen_at TEXT", "last_seen_at TEXT",
                    "is_open INTEGER", "closed_at TEXT"]


def _type(col: str) -> str:
    return "REAL" if col in FLOAT_COLUMNS else "INTEGER" if col in INT_COLUMNS else "TEXT"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS runs (
    run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT,
    finished_at  TEXT,
    input_file   TEXT,
    settings     TEXT,
    complete     INTEGER,   -- 1 = no --max-jobs cap, so jobs it didn't see can be marked closed
    us_only      INTEGER,
    n_companies  INTEGER,
    n_jobs       INTEGER
);
CREATE TABLE IF NOT EXISTS companies (
    company_key  TEXT PRIMARY KEY,   -- registered domain, e.g. pfizer.com
    name         TEXT,
    ats          TEXT,
    board_url    TEXT,
    first_seen   TEXT,
    last_seen    TEXT
);
CREATE TABLE IF NOT EXISTS company_snapshots (
    run_id            INTEGER REFERENCES runs(run_id),
    company_key       TEXT REFERENCES companies(company_key),
    status            TEXT,
    total_openings    INTEGER,
    us_openings       INTEGER,
    location_unknown  INTEGER,
    jobs_saved        INTEGER,
    rejected_requests TEXT,
    manual_check      TEXT,
    notes             TEXT,
    PRIMARY KEY (run_id, company_key)
);
CREATE TABLE IF NOT EXISTS jobs (
    job_uid      TEXT PRIMARY KEY,   -- company_key|ats|job_id
    company_key  TEXT REFERENCES companies(company_key),
    {", ".join(f"{c} {_type(c)}" for c in JOB_COLUMNS)},
    {", ".join(TRACKING_COLUMNS)}
);
""" + "".join(f"""
CREATE TABLE IF NOT EXISTS {table} (
    job_uid TEXT REFERENCES jobs(job_uid),
    {col}   TEXT,
    PRIMARY KEY (job_uid, {col})
);
""" for table, (col, _) in CHILD_TABLES.items())

# indexes and views come after the column upgrade, since they refer to newer columns
INDEXES_AND_VIEWS = "".join(f"CREATE INDEX IF NOT EXISTS idx_{table}_{col} ON {table}({col});\n"
                            for table, (col, _) in CHILD_TABLES.items()) + """
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company_key);
CREATE INDEX IF NOT EXISTS idx_jobs_open ON jobs(is_open);
CREATE VIEW IF NOT EXISTS v_open_jobs AS SELECT * FROM jobs WHERE is_open = 1;
CREATE VIEW IF NOT EXISTS v_latest_snapshots AS
    SELECT s.* FROM company_snapshots s
    JOIN (SELECT company_key, MAX(run_id) AS run_id FROM company_snapshots GROUP BY company_key) latest
      USING (company_key, run_id);
"""


def open_db(path: str) -> sqlite3.Connection:
    """Open (creating if needed) the database and bring its schema up to date."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        # columns added since the database was created (new Job fields, tracking columns)
        have = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        wanted = [(c, _type(c)) for c in JOB_COLUMNS] + [tuple(t.split()) for t in TRACKING_COLUMNS]
        for col, sql_type in wanted:
            if col not in have:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {sql_type}")
        conn.executescript(INDEXES_AND_VIEWS)
        conn.commit()
    except Exception:
        conn.close()
        raise
    return conn


def _value(col: str, v):
    if col in LIST_COLUMNS:
        return (" | " if col == "locations" else "; ").join(v or [])
    if isinstance(v, bool):
        return int(v)
    if v == "" and (col in FLOAT_COLUMNS or col in INT_COLUMNS):
        return None
    return v


def start_run(conn: sqlite3.Connection, input_file: str, settings: dict, complete: bool, us_only: bool) -> int:
    cur = conn.execute("INSERT INTO runs(started_at, input_file, settings, complete, us_only) VALUES (?,?,?,?,?)",
                       (_now(), input_file, json.dumps(settings, default=str), int(complete), int(us_only)))
    conn.commit()
    return cur.lastrowid


def save_run(conn: sqlite3.Connection, run_id: int, reports, complete: bool, us_only: bool) -> dict:
    """Write one run's results. Returns counts: new / updated / closed jobs, companies."""
    now = _now()
    cols = ["job_uid", "company_key"] + JOB_COLUMNS
    upsert = (f"INSERT INTO jobs ({', '.join(cols)}, first_seen_run, last_seen_run, first_seen_at, last_seen_at, "
              f"is_open, closed_at) VALUES ({', '.join('?' * len(cols))}, ?, ?, ?, ?, 1, NULL) "
              f"ON CONFLICT(job_uid) DO UPDATE SET "
              + ", ".join(f"{c} = excluded.{c}" for c in cols[1:])
              + ", last_seen_run = excluded.last_seen_run, last_seen_at = excluded.last_seen_at,"
                " is_open = 1, closed_at = NULL")
    stats = {"new": 0, "updated": 0, "closed": 0, "companies": 0}
    # a company may come from several input rows (pfizer.com and careers.pfizer.com): its unseen
    # jobs are closed only if every one of its collections succeeded and listed the whole board
    can_close: dict[str, bool] = {}
    for rep in reports:
        ok = rep.status == "ok" and getattr(rep, "listing_complete", True)
        can_close[rep.company_key] = can_close.get(rep.company_key, True) and ok
    with conn:  # one transaction for the whole run
        for rep in reports:
            key = rep.company_key
            stats["companies"] += 1
            conn.execute(
                "INSERT INTO companies(company_key, name, ats, board_url, first_seen, last_seen) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(company_key) DO UPDATE SET name = excluded.name, last_seen = excluded.last_seen, "
                "ats = CASE WHEN excluded.ats != '' THEN excluded.ats ELSE companies.ats END, "
                "board_url = CASE WHEN excluded.board_url != '' THEN excluded.board_url ELSE companies.board_url END",
                (key, rep.company, rep.ats, rep.board_url, now, now))
            prev = conn.execute("SELECT * FROM company_snapshots WHERE run_id = ? AND company_key = ?",
                                (run_id, key)).fetchone()
            snap = (run_id, key, rep.status, rep.total_openings, rep.us_openings, rep.location_unknown, len(rep.jobs),
                    rep.rejected_requests, rep.manual_check, "; ".join(rep.notes))
            if prev:  # a second input row for the same company: add its counts rather than overwrite them
                add = lambda a, b: None if a is None and b is None else (a or 0) + (b or 0)
                join = lambda a, b: "; ".join(x for x in (a, b) if x)
                snap = (run_id, key, prev[2] if prev[2] == rep.status else f"{prev[2]}+{rep.status}",
                        add(prev[3], snap[3]), add(prev[4], snap[4]), add(prev[5], snap[5]), add(prev[6], snap[6]),
                        join(prev[7], snap[7]), join(prev[8], snap[8]), join(prev[9], snap[9]))
            conn.execute("INSERT OR REPLACE INTO company_snapshots VALUES (?,?,?,?,?,?,?,?,?,?)", snap)

            # one query for which of this company's jobs exist already, then batched writes
            existing = {uid for (uid,) in conn.execute("SELECT job_uid FROM jobs WHERE company_key = ?", (key,))}
            rows, uids = [], []
            for job in rep.jobs:
                uid = f"{key}|{job.ats}|{job.job_id}"
                stats["updated" if uid in existing else "new"] += 1
                existing.add(uid)  # the same job listed twice counts once as new
                rows.append([uid, key] + [_value(c, getattr(job, c)) for c in JOB_COLUMNS] + [run_id, run_id, now, now])
                uids.append((uid,))
            conn.executemany(upsert, rows)
            # still on the board but not saved this run (e.g. its detail page failed, so it couldn't
            # be confirmed as US): it was seen, so it stays open; its stored details are left as they were
            saved = {u for (u,) in uids}
            conn.executemany("UPDATE jobs SET last_seen_run = ?, last_seen_at = ?, is_open = 1, closed_at = NULL "
                             "WHERE job_uid = ?",
                             [(run_id, now, uid) for uid in (f"{key}|{i}" for i in getattr(rep, "listed_ids", ()))
                              if uid not in saved])
            for table, (col, attr) in CHILD_TABLES.items():
                conn.executemany(f"DELETE FROM {table} WHERE job_uid = ?", uids)
                conn.executemany(f"INSERT OR IGNORE INTO {table}(job_uid, {col}) VALUES (?, ?)",
                                 [(f"{key}|{job.ats}|{job.job_id}", v) for job in rep.jobs
                                  for v in dict.fromkeys(getattr(job, attr) or [])])

        # after every report is saved: a complete, successful collection that no longer lists a job
        # means it closed; a US-only run only speaks for US jobs
        if complete:
            for key, ok in can_close.items():
                if not ok:
                    continue
                cur = conn.execute(
                    "UPDATE jobs SET is_open = 0, closed_at = ? WHERE company_key = ? AND is_open = 1 "
                    "AND last_seen_run < ?" + (" AND is_us = 1" if us_only else ""), (now, key, run_id))
                stats["closed"] += cur.rowcount
        conn.execute("UPDATE runs SET finished_at = ?, n_companies = ?, n_jobs = ? WHERE run_id = ?",
                     (now, stats["companies"], stats["new"] + stats["updated"], run_id))
    stats["total_jobs"], stats["open_jobs"] = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(is_open), 0) FROM jobs").fetchone()
    return stats
