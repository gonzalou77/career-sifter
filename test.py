#!/usr/bin/env python3
"""
test.py - check that Career Sifter is installed and working.

    python test.py            installation checks + every test_*.py file (offline, ~10 s)
    python test.py --live     also a short real run of both scripts on one company (needs internet)
    python test.py --r        also the R tests in tests/ (needs R; see DATABASE.md section 7)
    python test.py -k salary  only tests whose name contains "salary"

Exit code 0 means everything passed. Running the offline tests also builds tests/fixture.db,
the known-contents database the R tests use.
"""
from __future__ import annotations

import argparse
import csv
import glob
import importlib
import importlib.metadata
import inspect
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_MODULES = ["find_careers", "ats_clients", "classify", "job_details", "fetch_jobs", "database"]
MIN_PYTHON = (3, 10)
MIN_SQLITE = (3, 24, 0)  # INSERT ... ON CONFLICT DO UPDATE

results: list[tuple[str, str, str]] = []  # (section, name, "PASS"/"FAIL"/"SKIP"/"INFO" + detail)


def report(section: str, name: str, status: str, detail: str = "") -> None:
    results.append((section, name, status, detail))
    mark = {"PASS": "ok  ", "FAIL": "FAIL", "SKIP": "skip", "INFO": "info"}[status]
    print(f"  [{mark}] {name}" + (f"  - {detail}" if detail else ""))


# --------------------------------------------------------------------------- #
# 1. installation
# --------------------------------------------------------------------------- #

def check_installation() -> None:
    print("\n1. Installation")
    v = sys.version_info
    report("install", f"Python {v.major}.{v.minor}.{v.micro}",
           "PASS" if v[:2] >= MIN_PYTHON else "FAIL",
           "" if v[:2] >= MIN_PYTHON else f"need {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer (DATABASE.md step 1)")
    if sys.prefix == sys.base_prefix:
        report("install", "virtual environment", "INFO",
               "not running inside .venv - use .\\.venv\\Scripts\\python test.py if packages seem missing")

    # every package named in requirements.txt (comment lines are optional extras)
    with open(os.path.join(HERE, "requirements.txt"), encoding="utf-8") as f:
        required = [re.split(r"[<>=!~ ]", ln.strip(), maxsplit=1)[0] for ln in f
                    if ln.strip() and not ln.lstrip().startswith("#")]
    for pkg in required:
        try:
            report("install", f"package {pkg} {importlib.metadata.version(pkg)}", "PASS")
        except importlib.metadata.PackageNotFoundError:
            report("install", f"package {pkg}", "FAIL",
                   f"not installed - run: python -m pip install -r requirements.txt")

    try:
        importlib.metadata.version("playwright")
        report("install", "playwright (optional, for --render)", "PASS")
    except importlib.metadata.PackageNotFoundError:
        report("install", "playwright (optional, for --render)", "INFO", "not installed; only needed for --render")

    # SQLite: present, recent enough, and able to do what database.py does
    ver = tuple(int(x) for x in sqlite3.sqlite_version.split("."))
    try:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (k TEXT PRIMARY KEY, v INTEGER)")
        conn.execute("INSERT INTO t VALUES ('a', 1) ON CONFLICT(k) DO UPDATE SET v = excluded.v")
        conn.execute("INSERT INTO t VALUES ('a', 2) ON CONFLICT(k) DO UPDATE SET v = excluded.v")
        ok = conn.execute("SELECT v FROM t").fetchone() == (2,) and ver >= MIN_SQLITE
        conn.close()
        report("install", f"SQLite {sqlite3.sqlite_version} (built into Python)", "PASS" if ok else "FAIL",
               "" if ok else "need SQLite 3.24+ - install a current Python")
    except sqlite3.Error as e:
        report("install", f"SQLite {sqlite3.sqlite_version}", "FAIL", str(e))

    for mod in PROJECT_MODULES:
        try:
            importlib.import_module(mod)
            report("install", f"module {mod}.py imports", "PASS")
        except Exception as e:  # syntax errors, missing packages, ...
            report("install", f"module {mod}.py imports", "FAIL", f"{type(e).__name__}: {e}")

    try:
        with tempfile.NamedTemporaryFile(dir=HERE, prefix=".write-test-", delete=True):
            pass
        report("install", "project folder is writable (results, known_sites.csv, database)", "PASS")
    except OSError as e:
        report("install", "project folder is writable", "FAIL", str(e))


# --------------------------------------------------------------------------- #
# 2. test suite
# --------------------------------------------------------------------------- #

def run_test_files(name_filter: str) -> None:
    print("\n2. Tests (every test_*.py file)")
    for path in sorted(glob.glob(os.path.join(HERE, "test_*.py"))):
        module_name = os.path.splitext(os.path.basename(path))[0]
        try:
            module = importlib.import_module(module_name)
        except Exception as e:
            report("tests", f"{module_name}: import", "FAIL", f"{type(e).__name__}: {e}")
            continue
        tests = sorted((fn for name, fn in inspect.getmembers(module, inspect.isfunction)
                        if name.startswith("test_") and fn.__module__ == module_name),
                       key=lambda fn: fn.__code__.co_firstlineno)
        for fn in tests:
            label = f"{module_name}.{fn.__name__}"
            if name_filter and name_filter.lower() not in label.lower():
                continue
            start = time.perf_counter()
            try:
                fn()
                report("tests", label, "PASS", f"{time.perf_counter() - start:.1f}s")
            except Exception as e:
                frame = traceback.extract_tb(e.__traceback__)[-1]
                where = f"{os.path.basename(frame.filename)}:{frame.lineno}"
                report("tests", label, "FAIL", f"{type(e).__name__} at {where}: {str(e)[:200]}")


# --------------------------------------------------------------------------- #
# 3. live run (optional)
# --------------------------------------------------------------------------- #

def run_live() -> None:
    print("\n3. Live run (internet): benchling.com, both scripts, 3 jobs")
    with tempfile.TemporaryDirectory() as d:
        sites, results_csv, out = os.path.join(d, "sites.txt"), os.path.join(d, "results.csv"), os.path.join(d, "out")
        with open(sites, "w", encoding="utf-8") as f:
            f.write("benchling.com\n")
        steps = [
            ("find_careers.py finds the job board",
             [sys.executable, "find_careers.py", sites, "-o", results_csv, "--no-known"]),
            ("fetch_jobs.py collects and classifies jobs",
             [sys.executable, "fetch_jobs.py", results_csv, "--out", out, "--max-jobs", "3",
              "--db", os.path.join(d, "live.db")]),
        ]
        for label, cmd in steps:
            p = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=300)
            if p.returncode != 0:
                report("live", label, "FAIL", (p.stderr or p.stdout).strip().splitlines()[-1][:200])
                return
            if "find_careers" in cmd[1]:
                with open(results_csv, encoding="utf-8") as f:
                    row = next(csv.DictReader(f))
                ok = row["status"] == "listings_found" and row["ats"]
                report("live", label, "PASS" if ok else "FAIL",
                       f"{row['status']} [{row['ats']}] {row['listings_url']}" if ok else
                       f"{row['status']}: {row['notes'][:150]}")
                if not ok:
                    return
            else:
                with open(os.path.join(out, "summary.csv"), encoding="utf-8") as f:
                    row = next(r for r in csv.DictReader(f) if r["company"] != "ALL")
                ok = row["status"] == "ok" and int(row["us_openings"] or 0) > 0
                report("live", label, "PASS" if ok else "FAIL",
                       f"{row['total_openings']} openings, {row['us_openings']} US sampled, database written"
                       if ok else f"{row['status']}: {row['notes'][:150]}")


# --------------------------------------------------------------------------- #
# 4. R tests (optional)
# --------------------------------------------------------------------------- #

def find_rscript() -> str | None:
    found = shutil.which("Rscript")
    if found:
        return found
    candidates = sorted(glob.glob(r"C:\Program Files\R\R-*\bin\Rscript.exe"))  # R doesn't always add itself to PATH
    return candidates[-1] if candidates else None


def run_r() -> None:
    print("\n4. R tests (tests/run_r_tests.R against tests/fixture.db)")
    rscript = find_rscript()
    if not rscript:
        report("r", "R installed", "SKIP", "Rscript not found - see DATABASE.md section 7.1")
        return
    if not os.path.exists(os.path.join(HERE, "tests", "fixture.db")):
        report("r", "tests/fixture.db", "FAIL", "missing - run python test.py once without -k to build it")
        return
    p = subprocess.run([rscript, os.path.join("tests", "run_r_tests.R")], cwd=HERE, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=600)  # R prints UTF-8 box characters
    lines = [ln for ln in ((p.stdout or "") + (p.stderr or "")).splitlines() if ln.strip()]
    summary = next((ln for ln in reversed(lines) if "checks passed" in ln or "Error" in ln), lines[-1] if lines else "")
    report("r", "R test suite", "PASS" if p.returncode == 0 else "FAIL", summary.strip()[:200])
    if p.returncode != 0:
        print(p.stdout[-3000:], p.stderr[-2000:], sep="\n")


# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description="Check that Career Sifter is installed and working.")
    ap.add_argument("--live", action="store_true", help="also run both scripts on one real company (internet)")
    ap.add_argument("--r", action="store_true", help="also run the R tests (needs R)")
    ap.add_argument("-k", default="", help="only run tests whose name contains this text")
    args = ap.parse_args()

    os.chdir(HERE)
    sys.path.insert(0, HERE)
    print("Career Sifter self-test")
    check_installation()
    if not any(s == "install" and st == "FAIL" for s, _, st, _ in results):
        run_test_files(args.k)
    else:
        print("\n2. Tests skipped: fix the installation problems above first.")
    if args.live:
        run_live()
    if args.r:
        run_r()

    failed = [r for r in results if r[2] == "FAIL"]
    passed = sum(1 for r in results if r[2] == "PASS")
    print(f"\n{passed} passed, {len(failed)} failed" + (", " + str(sum(1 for r in results if r[2] == "SKIP"))
                                                       + " skipped" if any(r[2] == "SKIP" for r in results) else ""))
    for section, name, _, detail in failed:
        print(f"  FAIL [{section}] {name}: {detail}")
    if not failed:
        print("Everything is installed and working.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
