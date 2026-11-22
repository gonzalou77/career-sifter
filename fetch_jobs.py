#!/usr/bin/env python3
"""
fetch_jobs.py - pull every opening from the job boards found by find_careers.py,
down-select to US locations, and tally them by state, education requirement,
career level and optional keywords.

Usage:
    python find_careers.py sites.txt -o careers_results.csv
    python fetch_jobs.py careers_results.csv
    python fetch_jobs.py careers_results.csv --keywords "bioinformatics,CRISPR,python"
    python fetch_jobs.py careers_results.csv --keywords @keywords.txt --out output

Writes to the output folder:
    summary.csv        one row per company (+ ALL row): totals, US count, education & level breakdowns
    us_locations.csv   US openings per company and state
    jobs.csv           one row per US job with its classifications
    jobs.jsonl         same jobs including full description text (for later keyword work)
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from statistics import median

import database
from ats_clients import CLIENTS, Job, RobotsDisallowed
from classify import (EDU_NOT_STATED, EDU_ORDER, LEVEL_UNSPECIFIED, LEVELS, classify_education,
                      classify_level, infer_missing_states, min_years_experience)
from find_careers import make_session, robots_note, site_key
from job_details import extract_details, extract_salary

log = logging.getLogger("career_sifter.jobs")

USABLE_STATUSES = ("listings_found", "careers_page_found")
EDU_COLUMNS ={lvl: "edu_" + re.sub(r"[^a-z]+", "_", lvl.lower()).strip("_") for lvl in EDU_ORDER + [EDU_NOT_STATED]}
LEVEL_COLUMNS = {lvl: f"level_{lvl}" for lvl in LEVELS + [LEVEL_UNSPECIFIED]}


@dataclass
class CompanyReport:
    company: str
    ats: str
    board_url: str
    status: str = "ok"     # ok | no_ats | unsupported_ats | blocked | robots_disallowed | error
    rejected_requests: str = ""   # e.g. "jobs.x.com 403 Cloudflare x12"
    total_openings: int | None = None
    us_openings: int = 0
    location_unknown: int = 0
    jobs: list[Job] = field(default_factory=list)   # US jobs (or all, with --all-locations)
    notes: list[str] = field(default_factory=list)
    manual_check: str = ""  # what to open by hand when the openings couldn't be collected
    # the company's id in the database: its registered domain (pfizer.com), or for a job board pasted
    # as the input, the board itself (board:lilly.wd115.myworkdayjobs.com/lly) - never the ATS's domain
    company_key: str = ""
    # False when the board returned clearly fewer jobs than it reported, or none at all:
    # the database then marks nothing closed for this company
    listing_complete: bool = True
    # "ats|job_id" of every job the board listed this run, saved or not: a listed job whose location
    # couldn't be told (detail page failed) is still open, so the database must not close it
    listed_ids: set[str] = field(default_factory=set)


def load_keywords(spec: str | None) -> list[str]:
    if not spec:
        return []
    if spec.startswith("@"):
        with open(spec[1:], encoding="utf-8-sig") as f:
            items = [ln.split("#", 1)[0] for ln in f]
    else:
        items = spec.split(",")
    return list(dict.fromkeys(k.strip() for k in items if k.strip()))


def keyword_res(keywords: list[str]) -> dict[str, re.Pattern]:
    return {k: re.compile(r"(?<![A-Za-z0-9])" + re.escape(k) + r"(?![A-Za-z0-9])", re.I) for k in keywords}


def classify_job(job: Job, kw_res: dict[str, re.Pattern]) -> None:
    text = job.description
    job.education_mentioned, job.min_education = classify_education(text)
    job.min_years_experience = min_years_experience(text)
    job.career_level, job.level_source = classify_level(job.title, text, job.level_raw, job.min_years_experience)
    haystack = f"{job.title}\n{text}"
    job.keywords = [k for k, rx in kw_res.items() if rx.search(haystack)]

    # salary: the job board's own pay fields first, else the description
    for k, v in (job.meta.get("salary") or extract_salary(text)).items():
        setattr(job, k, v)
    for k, v in extract_details(text, job.title, job.employment_type, job.meta.get("arrangement", "")).items():
        setattr(job, k, v)
    if not job.work_arrangement and job.remote:
        job.work_arrangement = "remote"


def company_identity(input_url: str) -> tuple[str, str]:
    """(database key, company web domain) for an input URL.

    A homepage or careers page is keyed by its registered domain (pfizer.com). A job board pasted
    as the input is keyed by the board (same key as known_sites.csv), since its domain belongs to
    the ATS: every pasted Workday board would otherwise be 'myworkdayjobs.com'. The company's own
    domain is then unknown ("").
    """
    if not input_url.strip():
        return "", ""
    key = site_key(input_url.strip())
    if key.startswith("board:"):
        return key, ""
    return key, key


def process_company(row: dict, args: argparse.Namespace, kw_res: dict[str, re.Pattern]) -> CompanyReport:
    input_url = row.get("input_url", "")
    key, domain = company_identity(input_url)
    company = row.get("company") or domain or key
    ats = row.get("ats", "")
    board = row.get("listings_url") or row.get("careers_url", "")
    rep = CompanyReport(company=company, ats=ats, board_url=board, company_key=key or company.lower())
    finder_rejections = row.get("rejected_requests", "")

    if row.get("status") not in USABLE_STATUSES:  # blocked / not_found / error in find_careers
        rep.status = row.get("status") or "error"
        rep.rejected_requests = finder_rejections
        rep.manual_check = row.get("manual_check", "")
        rep.notes.append(f"careers page not found by find_careers: {row.get('notes', '')}".rstrip(": "))
        return rep
    if not ats:
        rep.status = "no_ats"
        rep.manual_check = row.get("manual_check") or f"no job board detected - open {board} in a browser"
        rep.notes.append("no job board detected - check careers page manually")
        return rep
    if ats not in CLIENTS:
        rep.status = "unsupported_ats"
        rep.manual_check = f"{ats} job boards aren't supported yet - open {board} in a browser to see the openings"
        rep.notes.append(f"{ats} not supported yet")
        return rep

    session = make_session()
    client = None
    try:
        client = CLIENTS[ats](board, company, session, delay=args.delay, timeout=args.timeout,
                              company_domain=domain, respect_robots=not args.ignore_robots)
        jobs = client.list_jobs(us_only=not args.all_locations)
    except RobotsDisallowed as e:
        rep.status = "robots_disallowed"
        rep.manual_check = robots_note(board)
        rep.notes.append(f"{e} - skipped; try again with the company's careers search URL")
        rep.rejected_requests = finder_rejections
        return rep
    except Exception as e:
        rejected = client is not None and bool(client.rejections)
        rep.status = "blocked" if rejected else "error"
        rep.notes.append(f"listing failed: {type(e).__name__}: {e}"[:300])
        rep.rejected_requests = join_rejections(finder_rejections, client.rejections if client else None)
        rep.manual_check = f"the job board could not be read - open {board} in a browser"
        return rep
    rep.total_openings = client.total_openings if client.total_openings is not None else len(jobs)
    rep.listed_ids = {f"{j.ats}|{j.job_id}" for j in jobs}
    if not jobs:
        rep.listing_complete = False
        rep.notes.append("the job board returned no jobs - nothing marked closed; rerun to confirm")
    elif not client.listing_complete(len(jobs)):
        rep.listing_complete = False
        rep.notes.append(f"listing cut short ({len(jobs)} of {client.expected_listed} jobs the board reported) - "
                         f"nothing marked closed")

    # Jobs whose location we can't tell from the listing need the detail call to decide.
    targets = jobs if args.all_locations else [j for j in jobs if j.is_us is not False]
    beyond_cap: list[Job] = []  # listed but not looked at in detail (--max-jobs)
    if args.max_jobs:
        if len(targets) > args.max_jobs:
            rep.notes.append(f"capped at {args.max_jobs} of {len(targets)} jobs (--max-jobs); US count beyond the "
                             f"cap is from listing locations only")
        targets, beyond_cap = targets[: args.max_jobs], targets[args.max_jobs:]

    def enrich(job: Job) -> None:
        if job.description and not job.meta.get("summary_only"):
            return
        try:
            client.enrich(job)
        except Exception as e:
            job.detail_error = f"{type(e).__name__}: {e}"[:200]

    with ThreadPoolExecutor(max_workers=args.detail_workers) as pool:
        list(pool.map(enrich, targets))
    try:
        client.finalize(targets)
    except Exception as e:
        rep.notes.append(f"post-processing failed: {type(e).__name__}: {e}"[:200])
    inferred = infer_missing_states(targets)
    if inferred:
        rep.notes.append(f"{inferred} job states inferred from site names")

    failed = sum(1 for j in targets if j.detail_error)
    if failed:
        rep.notes.append(f"{failed} job details could not be fetched (classified on title only)")

    selected = targets if args.all_locations else [j for j in targets if j.is_us]
    # counts cover the whole listing, not just the capped sample, so a --max-jobs run's
    # US count and us_pct still describe the company
    rep.location_unknown = sum(1 for j in targets + beyond_cap if j.is_us is None)
    rep.us_openings = sum(1 for j in targets + beyond_cap if j.is_us)
    if client.us_filtered and not args.max_jobs:
        rep.notes.append("US count from the job board's own country filter")
    for j in selected:
        classify_job(j, kw_res)
    rep.jobs = selected
    rep.rejected_requests = join_rejections(finder_rejections, client.rejections)
    return rep


def join_rejections(finder: str, client_counts: Counter | None) -> str:
    """Combine refusals seen by find_careers with those seen while collecting jobs."""
    parts = [finder] if finder else []
    if client_counts:
        parts.append("; ".join(f"{k} x{n}" for k, n in client_counts.most_common()))
    return "; ".join(parts)


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #

def summary_row(name: str, ats: str, status: str, total, us: int, unknown: int, jobs: list[Job],
                keywords: list[str], board: str = "", notes: str = "", rejected: str = "",
                manual: str = "") -> dict:
    us_jobs = [j for j in jobs if j.is_us]
    edu = Counter(j.min_education for j in us_jobs)
    lvl = Counter(j.career_level for j in us_jobs)
    states = Counter(s for j in us_jobs for s in j.states)
    row = {
        "company": name, "ats": ats, "status": status, "rejected_requests": rejected,
        "total_openings": "" if total is None else total,
        "us_openings": us,
        "us_pct": f"{100 * us / total:.0f}%" if total else "",
        "location_unknown": unknown,
        "top_us_states": "; ".join(f"{s}:{n}" for s, n in states.most_common(5)),
    }
    row.update({col: edu.get(lvl_name, 0) for lvl_name, col in EDU_COLUMNS.items()})
    row.update({col: lvl.get(lvl_name, 0) for lvl_name, col in LEVEL_COLUMNS.items()})
    paid = sorted(j.salary_annual_mid for j in us_jobs if j.salary_annual_mid and j.salary_currency == "USD")
    arrangement = Counter(j.work_arrangement for j in us_jobs)
    term = Counter(j.employment_term for j in us_jobs)
    row.update({
        "pay_disclosed": len(paid),
        "median_pay_annual_usd": round(median(paid)) if paid else "",
        "remote": arrangement.get("remote", 0), "hybrid": arrangement.get("hybrid", 0),
        "on_site": arrangement.get("on-site", 0),
        "internships": term.get("internship", 0), "fixed_term_or_contract": term.get("fixed-term", 0) + term.get(
            "contract", 0),
        "visa_sponsorship_not_offered": sum(1 for j in us_jobs if j.visa_sponsorship == "not offered"),
    })
    row.update({f"kw_{k}": sum(1 for j in us_jobs if k in j.keywords) for k in keywords})
    row.update({"board_url": board, "manual_check": manual, "notes": notes})
    return row


def write_outputs(reports: list[CompanyReport], keywords: list[str], out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)

    rows = [summary_row(r.company, r.ats, r.status, r.total_openings, r.us_openings, r.location_unknown,
                        r.jobs, keywords, r.board_url, "; ".join(r.notes), r.rejected_requests, r.manual_check)
            for r in reports]
    ok = [r for r in reports if r.status == "ok"]
    n_rejected = sum(1 for r in reports if r.rejected_requests)
    rows.append(summary_row("ALL", "", f"{len(ok)}/{len(reports)} ok",
                            sum(r.total_openings or 0 for r in ok), sum(r.us_openings for r in ok),
                            sum(r.location_unknown for r in ok), [j for r in ok for j in r.jobs], keywords,
                            rejected=f"{n_rejected} compan{'y' if n_rejected == 1 else 'ies'}" if n_rejected else ""))
    with open(os.path.join(out_dir, "summary.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    with open(os.path.join(out_dir, "us_locations.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["company", "state", "city", "openings"])
        for r in ok:
            # a multi-state job counts once in each state; its city is only shown when it has one state
            def city_label(j: Job) -> str:
                if len(j.states) > 1:
                    return "(multiple)"
                return j.city or ("(remote)" if j.remote else "(unknown)")

            places = Counter((s, city_label(j)) for j in r.jobs if j.is_us for s in (j.states or ["(not given)"]))
            for (s, city), n in sorted(places.items(), key=lambda kv: (kv[0][0] == "(not given)", kv[0][0], -kv[1])):
                w.writerow([r.company, s, city, n])

    job_cols = ["company", "ats", "title", "career_level", "level_source", "level_raw", "min_education",
                "education_mentioned", "min_years_experience",
                "salary_min", "salary_max", "salary_currency", "salary_period", "salary_annual_mid", "salary_source",
                "work_arrangement", "schedule", "employment_term", "contract_months", "shifts", "travel",
                "visa_sponsorship", "relocation", "bonus", "equity", "certifications",
                "keywords", "city", "states", "locations",
                "remote", "is_us", "department", "employment_type", "posted", "url", "job_id", "detail_error"]
    all_jobs = [j for r in reports for j in r.jobs]
    with open(os.path.join(out_dir, "jobs.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=job_cols, extrasaction="ignore")
        w.writeheader()
        for j in all_jobs:
            d = asdict(j)
            for k in ("education_mentioned", "keywords", "states", "certifications"):
                d[k] = "; ".join(d[k])
            d["locations"] = " | ".join(d["locations"])
            w.writerow(d)

    with open(os.path.join(out_dir, "jobs.jsonl"), "w", encoding="utf-8") as f:
        for j in all_jobs:
            d = asdict(j)
            d.pop("meta", None)
            f.write(json.dumps(d, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Collect and classify job openings from detected job boards.")
    ap.add_argument("input", nargs="?", default="careers_results.csv", help="output CSV from find_careers.py")
    ap.add_argument("--out", default="output", help="output folder (default: output)")
    ap.add_argument("--keywords", help='comma list ("python,CRISPR") or @file with one keyword per line')
    ap.add_argument("--all-locations", action="store_true", help="classify every job, not just US ones")
    ap.add_argument("--workers", type=int, default=4, help="companies processed in parallel")
    ap.add_argument("--detail-workers", type=int, default=3, help="parallel detail requests per company")
    ap.add_argument("--delay", type=float, default=0.5, help="pause before each request (per worker)")
    ap.add_argument("--timeout", type=float, default=20)
    ap.add_argument("--max-jobs", type=int, default=0, help="cap jobs per company (for quick tests)")
    ap.add_argument("--ignore-robots", action="store_true",
                    help="collect from job boards even when their robots.txt disallows scripts")
    ap.add_argument("--db", default="career_sifter.db",
                    help="SQLite database to create or add to (default career_sifter.db)")
    ap.add_argument("--no-db", action="store_true", help="don't write to the database (CSV/JSONL only)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")
    logging.getLogger("urllib3").setLevel(logging.ERROR)

    with open(args.input, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))  # unusable rows are kept so blocked sites stay on record
    if not any(r.get("status") in USABLE_STATUSES for r in rows):
        log.error("no usable rows in %s (run find_careers.py first)", args.input)
        return 1
    keywords = load_keywords(args.keywords)
    kw_res = keyword_res(keywords)
    by_key: dict[str, list[str]] = {}
    for r in rows:
        key = company_identity(r.get("input_url", ""))[0]
        if key:
            by_key.setdefault(key, []).append(r.get("input_url", ""))
    for key, urls in by_key.items():
        if len(urls) > 1:
            log.warning("note: %s appear to be the same company (%s); in the database they share one record, "
                        "and its jobs are only marked closed if every one of them is collected", " and ".join(urls), key)
    log.info("Collecting jobs for %d companies...\n", len(rows))

    reports: list[CompanyReport | None] = [None] * len(rows)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(process_company, row, args, kw_res): i for i, row in enumerate(rows)}
        for n, fut in enumerate(as_completed(futs), 1):
            r = reports[futs[fut]] = fut.result()
            rejected = f"  (rejected: {r.rejected_requests})" if r.rejected_requests else ""
            if r.status != "ok":
                log.info("[%d/%d] %-22s %-16s %s%s", n, len(rows), r.company[:22], r.status,
                         "; ".join(r.notes), rejected)
                continue
            lv = Counter(j.career_level for j in r.jobs if j.is_us)
            log.info("[%d/%d] %-22s %-16s total %-5s US %-4d | %s%s", n, len(rows), r.company[:22], r.ats,
                     r.total_openings, r.us_openings,
                     " ".join(f"{k}:{lv.get(k, 0)}" for k in LEVELS + [LEVEL_UNSPECIFIED]), rejected)

    write_outputs(reports, keywords, args.out)
    log.info("\nWrote summary.csv, us_locations.csv, jobs.csv, jobs.jsonl to %s/", args.out)

    if not args.no_db:
        created = not os.path.exists(args.db)
        complete, us_only = not args.max_jobs, not args.all_locations
        settings = {k: v for k, v in vars(args).items() if k not in ("db", "no_db", "verbose")}
        conn = database.open_db(args.db)
        try:
            run_id = database.start_run(conn, args.input, settings, complete, us_only)
            s = database.save_run(conn, run_id, reports, complete, us_only)
        finally:
            conn.close()
        log.info("%s database %s (run %d): %d new jobs, %d updated, %d closed%s; %d jobs in total, %d open",
                 "Created" if created else "Added to", args.db, run_id, s["new"], s["updated"], s["closed"],
                 "" if complete else " (capped run: none marked closed)", s["total_jobs"], s["open_jobs"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
