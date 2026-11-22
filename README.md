# Career Sifter

Finds the careers / job-listings page for a list of company websites
(pharma, biotech, ag, diagnostics, computational science, ...).

- **[ARCHITECTURE.md](ARCHITECTURE.md):** Mermaid diagrams (system overview, entry paths
  and workarounds, per-company collection, classes, database tables, tests).
- **[DATABASE.md](DATABASE.md):** the SQL database tutorial (Python, R, Docker).

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
```

Optional, for JavaScript-heavy sites:

```powershell
.\.venv\Scripts\python -m pip install playwright
.\.venv\Scripts\python -m playwright install chromium
```

Then check that everything is installed and working:

```powershell
.\.venv\Scripts\python test.py
```

It should end with `Everything is installed and working.` (See [Testing](#testing).)

## Usage

```powershell
.\.venv\Scripts\python find_careers.py sites.example.txt -o results.csv
```

Input is a `.txt` (one site per line, `#` comments allowed, optionally
`Company Name, URL`) or a `.csv` with a `url`/`website` column (and optional
`company` column).

The scan starts one of two ways depending on the URL (shown in the `input_type` column):

- **homepage** (`pfizer.com`): searches the homepage for a careers / jobs / join-us link,
  then follows it to the careers page and on to the job listings.
- **careers_page** (`https://careers.astrazeneca.com`, `https://www.medpace.com/careers/`,
  `https://hr.mit.edu/jobs`): starts at that page and goes straight to the listings.
  **The company homepage is never requested**, so a homepage that blocks scripts
  can't stop the scan. Detected from a careers/jobs subdomain or a careers-like path.
- **job_board** (`https://pfizer.wd1.myworkdayjobs.com/PfizerCareers`): a known job
  board is used as-is (`method` = `input-is-board`), no crawling. The company name is
  taken from the board URL unless you give one.

In a CSV you can give both: a `careers_url` column, when filled, makes that row start
from the careers page even if `url` holds the homepage:

```csv
company,url,careers_url
AbbVie,abbvie.com,https://careers.abbvie.com
Pfizer,pfizer.com,
```

If a homepage can't be used (refused, robots.txt, server error), the finder also tries
`careers.<domain>` / `jobs.<domain>` and then the usual job-board addresses.

**Search-page workaround:** if a careers page won't load, the finder tries that site's
job-search pages, the paths career-site platforms usually use (`/us/en/search-results`,
`/us/en`, `/en/jobs`, `/search-jobs`, `/jobs/search`, ...). Some sites refuse only their
root: `careers.lilly.com` answers 403, but `careers.lilly.com/us/en/search-results` loads
and links to Lilly's real Workday board. It stops after 3 refusals in a row (the whole
host is then probably blocked). You can also paste a search URL directly as the
careers page.

### Saved sites: discover once, reuse after

Every successful scan is saved to `known_sites.csv` (one row per company, keyed by its
domain, so `pfizer.com`, `www.pfizer.com` and `careers.pfizer.com` share an entry). On later
runs with the same input list, each saved company starts straight from its **saved job board**
(or its saved careers page if no board was found), so the homepage is not needed.

- If a saved URL stops working (redesign, board moved), that company is rediscovered from
  the input URL and its entry is updated; the `source` column shows `rediscovered`.
- Failed scans never overwrite a working saved entry.
- **Companies with no usable job board are remembered too** (careers page without a
  board, blocked, not found). The `manual_check` column says which page to open by hand,
  e.g. *"no job board detected - open https://www.schrodinger.com/company/careers/ in a
  browser, find the openings and paste the job listings URL into listings_url in
  known_sites.csv"*. Later runs skip these (0 requests) instead of repeating the same
  search, until `--recheck-days` (default 30) have passed or you use `--refresh`.
  Once you paste the job board into `listings_url`, the next run uses it
  (`method` = `manual (known_sites.csv)`).
- The `requests` column shows how many HTTP requests each company cost. In testing, a
  rerun of 5 companies dropped from 101 requests to 6.
- `manual_check` also appears in `fetch_jobs.py`'s `summary.csv`, including for job
  boards that are unsupported, unreadable, or ask scripts not to read them.
- `source` in the results is `known`, `discovered` or `rediscovered`.
- A filled `careers_url` in the input always wins over a saved entry.
- `--refresh` rediscovers everything; `--known other.csv` uses another file; `--no-known` turns
  saving off.
- The file is plain CSV: edit it by hand, e.g. paste a company's job board into `listings_url`.
  It is git-ignored.

| Flag | Default | Purpose |
|---|---|---|
| `--workers` | 8 | sites scanned in parallel |
| `--delay` | 0.5 | seconds between requests to the same site |
| `--timeout` | 15 | per-request timeout |
| `--verify` | 3 | top candidate links to fetch and check |
| `--render` | off | headless browser fallback for JS-rendered pages |
| `--ignore-robots` | off | skip robots.txt checks |

## How it finds the page

1. Scores every homepage link by text ("Careers", "Join us", "See openings", ...),
   URL path (`/careers`, `/jobs`), subdomain (`careers.`, `jobs.`) and whether it
   points at a known applicant-tracking system (Workday, Greenhouse, Lever, iCIMS,
   SmartRecruiters, Taleo, SuccessFactors, Ashby, Eightfold, Phenom, ...).
2. Falls back to probing common paths, then the sitemap.
3. Fetches the top candidates and scores their content (JobPosting schema,
   ATS links/embeds, "search jobs" phrases, page title).
4. Follows the careers landing page through to the actual listings and
   normalises ATS links to the board root (skipping employee-only portals and login pages).
5. If no job board is visible (many load from JavaScript), tries the usual board
   addresses built from the domain name: Workday (`<name>.wdN.myworkdayjobs.com`,
   locating the data centre first, then common site names), `careers-<name>.icims.com`,
   `boards.greenhouse.io/<name>`, etc. These rows are marked `ats-probe` in `method`;
   check they belong to the right company. If a Workday tenant exists but its site
   name can't be guessed, the notes say so; paste the board URL as input instead.
6. If the homepage can't be used, starts from `careers.<domain>` / `jobs.<domain>`.

Tests: `python test_find_careers.py` (input detection) and `python test_classify.py`.

For universities, student "Career Center" pages are scored down in favour of
employment / HR job pages, and academic ranks (Assistant/Associate Professor,
Professor, Dean, Provost, Chair) feed the career-level tagging.

## Output columns

`status` is `listings_found`, `careers_page_found`, `not_found`, `blocked` (the homepage
refused us) or `error`. `rejected_requests` records every host that refused a request,
with the HTTP status and the bot-protection service when recognisable, e.g.
`www.lilly.com 403 Cloudflare` (also Akamai, CloudFront, AWS WAF, Imperva, DataDome,
CAPTCHA). Refusals never stop the run; they are just recorded.
`careers_url` is the landing page, `listings_url` the page/board with openings,
`ats` the detected platform, `score` a relative confidence, `method` which step
found it, `top_candidates` the runner-up links for manual review.

Keyword weights, ATS patterns and probe paths are at the top of `find_careers.py`.

## Step 2: collect and classify openings

```powershell
.\.venv\Scripts\python fetch_jobs.py careers_results.csv --keywords "python,bioinformatics,GMP"
```

For every company whose job board is supported (Workday, Greenhouse, Lever, Ashby,
SmartRecruiters, Eightfold, Phenom, iCIMS, Attrax, Oracle Recruiting Cloud, PeopleAdmin) it:

1. Counts **all openings** worldwide.
2. Down-selects to **US openings**, using the board's own country filter when it has
   one (Workday) or the job's structured/parsed location otherwise.
3. Fetches each US job's description and tags:
   - **state / city**
   - **minimum education** mentioned (High school/GED → Associate → Bachelor's →
     Master's → PhD / Professional doctorate, or Not stated)
   - **minimum years of experience** mentioned
   - **career level**: early / mid / senior / executive / unspecified, from the title
     first, then the board's own level label, then years of experience
   - any `--keywords` found in the title or description

Output (in `output/`):

| File | Contents |
|---|---|
| `summary.csv` | per company + ALL: status, rejected requests (from both steps, with counts), total, US count and %, top states, education and level counts, pay disclosed / median annual pay, remote / hybrid / on-site, internships, fixed-term or contract, visa sponsorship not offered, keyword counts, manual check. Blocked companies stay listed. |
| `us_locations.csv` | US openings per company / state / city |
| `jobs.csv` | one row per US job with all tags, details and its URL |
| `jobs.jsonl` | the same jobs including full description text |

### SQL database

Every run also saves the jobs into a SQLite database, `career_sifter.db`. It's created on
the first run and added to on every later run: new jobs are inserted, existing ones
updated, and jobs a complete run no longer sees are marked closed. Nothing extra needs to
be installed, since SQLite is built into Python. `--db other.db` picks another file and
`--no-db` skips it.

**See [DATABASE.md](DATABASE.md)** for a step-by-step tutorial: installing Python if
needed, creating and growing the database, tools for looking at it (DB Browser for
SQLite, the `sqlite3` command line, pandas), example queries, what each table holds,
analysing it from **R** (DBI/RSQLite, dplyr, ggplot2), and inspecting it with **Docker**
(sqlite-web, Datasette, sqlite3 in a container).

### Details pulled from each posting (`job_details.py`)

Taken from the job board's own fields when it has them (Ashby/Lever pay, schema.org
`baseSalary`, Attrax's salary and "Work location type", Oracle/Eightfold/Lever/Ashby
workplace type), otherwise from the description text. Anything a posting doesn't
state is left empty, not guessed.

| Column | Values |
|---|---|
| `salary_min`, `salary_max`, `salary_currency`, `salary_period` | e.g. 72000 / 189200 / USD / year; hourly pay keeps `hour` |
| `salary_annual_mid` | midpoint per year (hourly x 2080), for comparing |
| `salary_source` | `job board` or `description` |
| `work_arrangement` | remote / hybrid / on-site |
| `schedule` | full-time / part-time |
| `employment_term`, `contract_months` | permanent / fixed-term / contract / internship, and length if stated |
| `shifts` | day, night, weekend, 1st/2nd/3rd, 12-hour, rotating ... |
| `travel` | "up to 25%", none, or required |
| `visa_sponsorship` | offered / not offered |
| `relocation` | offered / possible ("may be available") / not offered |
| `bonus`, `equity` | yes when mentioned |
| `certifications` | PMP, Six Sigma, ASQ, CCRP/ACRP, RAC, ASCP/MLS, RN, pharmacist, vet tech, veterinary or medical licence, AALAS, crop adviser, CPA, PE, cloud, CISSP, PHR/SHRM, CDL |

Bonus and equity are often standard wording on every posting of a company, so they
say more about the company than the role. Checks: `python test_job_details.py`.

Useful flags: `--all-locations` (tag non-US jobs too), `--max-jobs 20` (quick test),
`--keywords @keywords.txt` (one per line), `--delay`, `--workers`, `--detail-workers`.

The tagging rules live in `classify.py`; `python test_classify.py` checks them.

iCIMS covers both classic portals (`careers-<co>.icims.com`, read from the search
pages plus each job's schema.org data) and iCIMS career sites (`careers.<co>.com`,
read from their `/api/jobs` feed); a portal that redirects to a career site is
followed.

All requests identify honestly as `CareerSifter/0.1 (job-market research script)`
rather than imitating a browser (iCIMS rejects browser-style agents from scripts).

Other platforms:
- **Attrax** (e.g. careers.abbvie.com): job tiles from `/<lang>/jobs`, descriptions from
  each job page's schema.org data; the site's own "Experience Level" feeds career level.
- **Oracle Recruiting Cloud** (e.g. careersearch.stanford.edu): the site's public
  `recruitingCE` REST API.
- **PeopleAdmin** (US universities, e.g. jobs.ncsu.edu): the `/postings/search.atom`
  feed plus each posting's field table. Postings count as US; the state is filled
  only when a location field or the description names one.

US jobs that only name a site ("TARRYTOWN", "Lincoln") get their state from the same
company's other postings ("Tarrytown, NY") or from "<city>, <state>" in the description.

**robots.txt:** both scripts respect it by default. Some job boards disallow scripts:
SmartRecruiters' API, Eightfold's API and PeopleAdmin's default robots.txt all do.
Those companies are reported as `robots_disallowed` in `summary.csv` rather than
collected. `--ignore-robots` (on either script) overrides this; use it only if you
are comfortable doing so for the sites concerned.

Known gaps: Taleo and SuccessFactors boards are reported as `unsupported_ats`;
companies with no detected board are `no_ats`. Niche university systems (Peopleclick,
Clinch, Paradox, custom portals) are not supported. Some Workday boards
list bare site names ("Lincoln") with no state, so those jobs show under
`(not given)` with the site name as city.

## Testing

### Python: `test.py`

One command checks that everything is installed and working:

```powershell
.\.venv\Scripts\python test.py
```

1. **Installation:** Python version, every package in `requirements.txt`, Playwright
   (optional, only for `--render`), Python's built-in SQLite (version, and a real
   insert-or-update), all six project modules import, and the folder is writable.
2. **Tests:** runs every `test_*.py` file and reports each test as ok / FAIL:

   | File | Covers |
   |---|---|
   | `test_classify.py` | US location parsing, education, career level, experience, state inference |
   | `test_job_details.py` | salary, work arrangement, term, shifts, travel, visa, relocation, certifications |
   | `test_find_careers.py` | input types, CSV loading, saved-sites file, manual-check notes |
   | `test_pipeline.py` | link scoring, job-board detection and links, refusal detection, location / schema.org helpers, and a full offline run (fake job board → classification → CSVs → database) |
   | `test_database.py` | creating and adding to the database, closing jobs, schema upgrades; builds `tests/fixture.db` |

The offline tests need no internet and take a few seconds. It ends with
`Everything is installed and working.` and exits with code 0, or lists what failed and
exits with 1.

| Option | Adds |
|---|---|
| `--live` | a real run of both scripts on one company (benchling.com, 3 jobs); needs internet |
| `--r` | the R tests below (needs R and its packages) |
| `-k text` | only tests whose name contains `text`, e.g. `-k salary` |

Each `test_*.py` file also runs on its own, e.g. `python test_job_details.py`. A new
`test_*.py` file is picked up by `test.py` automatically.

### R: `tests/run_r_tests.R`

Tests for working with the database from R (DATABASE.md section 7), written with
**testthat**. Install the packages once, in R:

```r
install.packages(c("testthat", "DBI", "RSQLite", "dplyr", "dbplyr", "ggplot2"))
```

Then, from the project folder:

```powershell
Rscript tests/run_r_tests.R                     # against tests/fixture.db (run python test.py first)
Rscript tests/run_r_tests.R career_sifter.db    # against your real database
```

(`python test.py --r` does the first one for you. If `Rscript` isn't on PATH, use its
full path, e.g. `& "C:\Program Files\R\R-4.6.1\bin\Rscript.exe"`.)

- **Any database:**
  - the expected tables and views exist;
  - data quality: each job appears once, salary minimum ≤ maximum, valid pay periods, consistent open/closed dates, open jobs have a link, valid US state codes, no orphaned rows, documented career levels;
  - every tutorial query runs;
  - tables load as data frames with dates that convert;
  - dplyr matches SQL;
  - the tutorial's ggplot chart builds.
- **`tests/fixture.db` only:** exact answers on known contents. These include open jobs per company, top states, average pay by level, education counts, a job closed between runs, and a job updated across runs keeping its first-seen date. They're skipped on other databases.

`tests/fixture.db` is built by `python test.py` (from `test_database.py`) and is
git-ignored like other databases.
