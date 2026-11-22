# Database tutorial

`fetch_jobs.py` saves every job it collects into a SQL database, so results build up
across runs and can be analysed with SQL. The first run creates the database; every later
run adds to it.

The database is **SQLite**: the whole database is one file (`career_sifter.db`), and there
is no database server to install, start or log in to.

**You do not need to install anything extra to create or update the database.** SQLite is
built into Python. You only need a separate tool if you want to *look at* the database or
run your own SQL queries (step 4).

---

## 1. Check Python (and install it if needed)

Open PowerShell (Windows) or Terminal (macOS/Linux) and run:

```powershell
python --version
```

If you see `Python 3.10` or newer, go to step 2.

If you get an error, or a version below 3.10, install Python:

- **Windows:** run
  ```powershell
  winget install --id Python.Python.3.12 -e
  ```
  or download the installer from https://www.python.org/downloads/ and, on the first
  screen, tick **"Add python.exe to PATH"**. Then close and reopen PowerShell.
- **macOS:** `brew install python` (with Homebrew), or the installer from python.org.
- **Linux (Debian/Ubuntu):** `sudo apt install python3 python3-venv`.

Confirm Python can use SQLite (it can, on every standard install):

```powershell
python -c "import sqlite3; print('SQLite', sqlite3.sqlite_version)"
```

You should see something like `SQLite 3.49.1`.

## 2. Set up the project (once)

From the `career sifter` folder:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
```

On macOS/Linux the second line is `.venv/bin/python -m pip install -r requirements.txt`
(use `.venv/bin/python` wherever these instructions say `.\.venv\Scripts\python`).

## 3. Create the database, then add to it

```powershell
.\.venv\Scripts\python find_careers.py sites.example.txt -o careers_results.csv
.\.venv\Scripts\python fetch_jobs.py careers_results.csv
```

The last line of the output tells you what happened. For example (your numbers will differ):

```
Created database career_sifter.db (run 1): 1563 new jobs, 0 updated, 0 closed; 1563 jobs in total, 1563 open
```

Run the same two commands again later (tomorrow, next week) and the database is
**added to**, not replaced:

```
Added to database career_sifter.db (run 2): 85 new jobs, 1470 updated, 93 closed; 1648 jobs in total, 1555 open
```

- **new**: jobs not seen before.
- **updated**: jobs seen before. Their details are refreshed and `last_seen` moves forward.
- **closed**: jobs a previous run saw that this run's board no longer lists.

A job is only marked closed when the run can be sure it is gone. That means the company's board was
read successfully and returned (nearly) all the jobs it reported: at least 95% of its own total, and
at least one job. A blocked board, a listing cut short by an empty page, or a board that suddenly
returns nothing leaves the company's jobs open, with a note in `company_snapshots.notes`. If one
company appears twice in the input (`pfizer.com` and `careers.pfizer.com`), its jobs are closed only
when both rows were collected. A job the board still lists but that couldn't be saved this run (its
detail page failed, so its location couldn't be confirmed) also stays open: its `last_seen` moves
forward and its stored details are kept.

Options:

| Option | Effect |
|---|---|
| `--db other.db` | use a different database file (created if it doesn't exist) |
| `--no-db` | don't touch the database; CSV/JSONL output only |
| `--max-jobs N` | quick test run. It still saves to the database, but **marks nothing closed**, since it didn't see every job. |

Back up the database by copying `career_sifter.db`. It is a normal file. It is git-ignored,
so it isn't uploaded to GitHub.

## 4. Look at the database (optional tools)

Pick one. For R see [section 7](#7-using-the-database-from-r). To avoid installing
anything except Docker, see [section 8](#8-inspecting-the-database-with-docker).

### Option A: DB Browser for SQLite (point-and-click, easiest)

A free desktop app for browsing tables, running SQL and exporting to CSV/Excel.

- **Windows:** `winget install --id DBBrowserForSQLite.DBBrowserForSQLite -e`
  (or download from https://sqlitebrowser.org/dl/)
- **macOS:** `brew install --cask db-browser-for-sqlite`
- **Linux:** `sudo apt install sqlitebrowser`

Open it, choose **Open Database**, and pick `career_sifter.db`. Then:

- **Browse Data** tab: scroll through any table.
- **Execute SQL** tab: paste a query from step 5 and press ▶ (or Ctrl+Enter).
- **File → Export → Table(s) as CSV file**: open the results in Excel.

Close DB Browser (or at least don't leave unsaved edits) while `fetch_jobs.py` is
running, so the script can write to the file.

### Option B: the `sqlite3` command-line tool

- **Windows:** `winget install --id SQLite.SQLite -e`, then close and reopen PowerShell.
  (Or download the "sqlite-tools-win-x64" zip from https://sqlite.org/download.html,
  unzip it, and run `sqlite3.exe` from that folder.)
- **macOS:** already installed.
- **Linux:** `sudo apt install sqlite3`

```powershell
sqlite3 career_sifter.db
```

Then, at the `sqlite>` prompt:

```sql
.tables                 -- list the tables
.headers on
.mode column            -- readable columns
SELECT company_key, COUNT(*) FROM v_open_jobs GROUP BY company_key;
.mode csv
.output open_jobs.csv   -- send the next result to a CSV file
SELECT * FROM v_open_jobs;
.output stdout
.quit
```

### Option C: Python / pandas (for analysis)

```python
import sqlite3
import pandas as pd

conn = sqlite3.connect("career_sifter.db")
jobs = pd.read_sql("SELECT * FROM v_open_jobs", conn)
print(jobs.groupby("career_level")["salary_annual_mid"].median())
```

(`pip install pandas` first if you don't have it.)

## 5. Example queries

These were all tested on a real database built by these scripts.

**Open jobs per company**
```sql
SELECT company_key, COUNT(*) AS open_jobs
FROM v_open_jobs GROUP BY company_key ORDER BY open_jobs DESC;
```

**Top states**
```sql
SELECT s.state, COUNT(*) AS jobs
FROM job_states s JOIN v_open_jobs j USING (job_uid)
GROUP BY s.state ORDER BY jobs DESC LIMIT 10;
```

**Average pay by career level** (annual midpoint; hourly pay converted at 2,080 h/year)
```sql
SELECT career_level, COUNT(*) AS jobs_with_pay, ROUND(AVG(salary_annual_mid)) AS avg_annual_mid
FROM v_open_jobs WHERE salary_annual_mid IS NOT NULL
GROUP BY career_level ORDER BY avg_annual_mid;
```

**Lowest degree mentioned**
```sql
SELECT min_education, COUNT(*) AS jobs
FROM v_open_jobs GROUP BY min_education ORDER BY jobs DESC;
```

**PhD-level openings with pay, highest first**
```sql
SELECT j.company, j.title, j.salary_min, j.salary_max, j.work_arrangement, j.url
FROM v_open_jobs j JOIN job_education e USING (job_uid)
WHERE e.level = 'PhD' AND j.salary_min IS NOT NULL
ORDER BY j.salary_max DESC LIMIT 20;
```

**Jobs that are new in the latest run**
```sql
SELECT company_key, COUNT(*) AS new_jobs FROM jobs
WHERE first_seen_run = (SELECT MAX(run_id) FROM runs)
GROUP BY company_key;
```

**A company's US openings over time**
```sql
SELECT r.run_id, substr(r.started_at, 1, 10) AS day, s.us_openings, s.total_openings
FROM company_snapshots s JOIN runs r USING (run_id)
WHERE s.company_key = 'lilly.com' ORDER BY r.run_id;
```

**How long closed jobs stayed open**
```sql
SELECT company_key, title,
       ROUND(julianday(closed_at) - julianday(first_seen_at)) AS days_listed
FROM jobs WHERE is_open = 0 ORDER BY days_listed DESC LIMIT 20;
```

## 6. What's in the database

| Table / view | One row per | Key columns |
|---|---|---|
| `runs` | run of `fetch_jobs.py` | `run_id`, `started_at`, `input_file`, `complete` (1 = not capped), `n_jobs` |
| `companies` | company | `company_key` (domain, e.g. `pfizer.com`; for a job board pasted as the input, the board itself, e.g. `board:lilly.wd115.myworkdayjobs.com/lly`), `name`, `ats`, `board_url`, `first_seen`, `last_seen` |
| `company_snapshots` | company per run | `status`, `total_openings`, `us_openings`, `manual_check`, `rejected_requests` |
| `jobs` | job | `job_uid` (company + platform + board job id), every column of `jobs.csv` plus the full `description`, `first_seen_at`, `last_seen_at`, `is_open`, `closed_at` |
| `job_states` | job x state | `job_uid`, `state` |
| `job_education` | job x degree mentioned | `job_uid`, `level` |
| `job_certifications` | job x certification | `job_uid`, `certification` |
| `job_keywords` | job x `--keywords` match | `job_uid`, `keyword` |
| `v_open_jobs` | open job | `jobs` filtered to `is_open = 1` |
| `v_latest_snapshots` | company | each company's most recent snapshot |

Multi-value fields (states, degrees, certifications, keywords) are also stored as text in
`jobs` ("IN; NJ"), but the one-row-per-value tables are what you `JOIN` and `GROUP BY` on.

When the scripts gain new job columns, an existing database is upgraded automatically
the next time `fetch_jobs.py` runs. Nothing needs to be recreated.

## 7. Using the database from R

R reads the database directly with the **DBI** and **RSQLite** packages; no other
database software is needed. (Tested with R 4.6.)

### 7.1 Install R (and optionally RStudio)

Check first. In PowerShell or Terminal:

```powershell
Rscript --version
```

If that isn't recognised:

- **Windows:**
  ```powershell
  winget install --id RProject.R -e
  winget install --id Posit.RStudio -e      # optional: the RStudio editor
  ```
  or download R from https://cran.r-project.org/ and RStudio from https://posit.co/download/rstudio-desktop/.
  Close and reopen PowerShell afterwards. If `Rscript` is still not recognised, call it by
  its full path, e.g. `& "C:\Program Files\R\R-4.6.1\bin\Rscript.exe"`. RStudio finds R by
  itself either way.
- **macOS:** `brew install --cask r` and, optionally, `brew install --cask rstudio`.
- **Linux (Debian/Ubuntu):** `sudo apt install r-base`.

### 7.2 Install the packages (once)

In the R console (RStudio's Console pane, or `R` in a terminal):

```r
install.packages(c("DBI", "RSQLite"))               # needed
install.packages(c("dplyr", "dbplyr", "ggplot2"))   # optional: tidy queries and plots
```

### 7.3 Connect and run SQL

Point R at the project folder first: in RStudio use **Session → Set Working Directory →
Choose Directory…** and pick the `career sifter` folder, or run
`setwd("C:/Users/<you>/.../career sifter")` (forward slashes). Alternatively, give the
full path to the database in `dbConnect`.

```r
library(DBI)

con <- dbConnect(RSQLite::SQLite(), "career_sifter.db")
dbListTables(con)                  # the tables and views from section 6
dbListFields(con, "jobs")          # the columns of a table

open_by_company <- dbGetQuery(con, "
  SELECT company_key, COUNT(*) AS open_jobs
  FROM v_open_jobs
  GROUP BY company_key
  ORDER BY open_jobs DESC")
open_by_company

# values go in with ? placeholders and params = list(...)
lilly <- dbGetQuery(con, "
  SELECT title, salary_min, salary_max, url
  FROM v_open_jobs
  WHERE company_key = ? AND salary_min IS NOT NULL",
  params = list("lilly.com"))
```

Any query from section 5 works inside `dbGetQuery(con, "...")`.

### 7.4 Work with a whole table as a data frame

```r
jobs <- dbReadTable(con, "v_open_jobs")     # every open job, every column
dim(jobs)
aggregate(salary_annual_mid ~ career_level, data = jobs, FUN = median)

# dates are stored as text: convert when you need them
jobs$first_seen <- as.Date(substr(jobs$first_seen_at, 1, 10))
table(jobs$first_seen)
```

`is_open`, `is_us` and `remote` are stored as 1/0; empty values come back as `NA`.

### 7.5 dplyr: write R, run as SQL

With **dbplyr**, dplyr code runs inside the database and only the result is pulled into R
with `collect()`. This is handy once the database gets large.

```r
library(dplyr)
library(dbplyr)

open_jobs <- tbl(con, "v_open_jobs")

pay_by_level <- open_jobs |>
  filter(!is.na(salary_annual_mid)) |>
  group_by(career_level) |>
  summarise(jobs = n(), avg_pay = mean(salary_annual_mid, na.rm = TRUE)) |>
  arrange(avg_pay) |>
  collect()

# multi-value fields: join the one-row-per-value table
top_states <- tbl(con, "job_states") |>
  inner_join(open_jobs, by = "job_uid") |>
  count(state, sort = TRUE) |>
  head(10) |>
  collect()
```

(`show_query()` in place of `collect()` prints the SQL that dplyr wrote.)

### 7.6 A plot and a CSV

```r
library(ggplot2)

pay <- open_jobs |>
  filter(!is.na(salary_annual_mid)) |>
  select(company_key, career_level, salary_annual_mid) |>
  collect()
pay$career_level <- factor(pay$career_level,
                           levels = c("early", "mid", "senior", "executive", "unspecified"))

ggplot(pay, aes(x = career_level, y = salary_annual_mid)) +
  geom_boxplot() +
  scale_y_continuous(labels = scales::dollar) +
  labs(x = "Career level", y = "Annual pay (midpoint)", title = "Pay by career level, open US jobs")
ggsave("pay_by_level.png", width = 7, height = 4)

write.csv(open_by_company, "open_by_company.csv", row.names = FALSE)
```

### 7.7 Disconnect when done

```r
dbDisconnect(con)
```

Reading from R while nothing else is writing is always safe. Disconnect before running
`fetch_jobs.py` if R is in the middle of a long query.

To rerun an analysis without RStudio, save the code as e.g. `analysis.R` and run
`Rscript analysis.R` from the project folder.

### 7.8 Test the database from R

The project includes a **testthat** test suite for everything in this section. Run it
after installing the packages (`install.packages(c("testthat", "DBI", "RSQLite",
"dplyr", "dbplyr", "ggplot2"))`):

```powershell
Rscript tests/run_r_tests.R career_sifter.db    # your database
Rscript tests/run_r_tests.R                     # the known-contents test database
```

Pointed at your database, it checks:
- the tables are there;
- data quality: each job appears once, salary minimum ≤ maximum, the annual pay midpoint matches its range and period, consistent open/closed dates and run numbers, every open job has a link, valid state codes that agree with the `states` column, every row points at an existing company and run;
- no company is keyed by a job-board platform's domain (e.g. `myworkdayjobs.com`), which would mean several companies had been merged; a database made before this was fixed can show it, and rerunning `fetch_jobs.py` doesn't repair old rows;
- every query in this tutorial runs;
- dplyr agrees with SQL;
- the chart builds.

It ends with `all R database checks passed`. The known-contents database
(`tests/fixture.db`) is built by `python test.py`, and tests exact answers too. See the
README's *Testing* section.

## 8. Inspecting the database with Docker

If you'd rather not install database tools on your computer, Docker can run them in a
throw-away container that opens your `career_sifter.db`. Nothing is installed except
Docker itself, and the container is removed when you stop it (`--rm`).

> These commands use public images (`coleifer/sqlite-web`, `datasetteproject/datasette`,
> `alpine`). They weren't run on the development machine, which has no Docker. If an
> image's defaults have changed, see its page on Docker Hub.

### 8.1 Install Docker

- **Windows:** `winget install --id Docker.DockerDesktop -e`, or download Docker Desktop
  from https://www.docker.com/products/docker-desktop/. Start Docker Desktop and wait until
  it says it's running. It may ask to enable WSL 2 and restart.
- **macOS:** `brew install --cask docker`, then open Docker from Applications.
- **Linux:** follow https://docs.docker.com/engine/install/ for your distribution.

Check it works:

```powershell
docker run --rm hello-world
```

### 8.2 Run the commands from the project folder

All commands below share the current folder with the container (`-v "${PWD}:/data"`).
`cd` into the `career sifter` folder first. Keep the quotes: the folder name has a space.

| Shell | Current-folder syntax |
|---|---|
| PowerShell (Windows) | `"${PWD}:/data"` (as written below) |
| macOS / Linux / Git Bash | `"$(pwd):/data"` |

### 8.3 Option 1: sqlite-web (browse and query in your web browser)

```powershell
docker run -it --rm -p 8080:8080 -v "${PWD}:/data" -e SQLITE_DATABASE=career_sifter.db coleifer/sqlite-web
```

Open http://localhost:8080. You can browse each table, see its structure, run SQL (the
**Query** tab) and export results. Stop it with **Ctrl+C** in the terminal.

### 8.4 Option 2: Datasette (explore, facet, export to CSV/JSON)

```powershell
docker run --rm -p 8001:8001 -v "${PWD}:/data" datasetteproject/datasette datasette -p 8001 -h 0.0.0.0 /data/career_sifter.db
```

Open http://localhost:8001. Datasette is built for exploring data: filter columns, facet
by `company_key`, `career_level` or `state`, run SQL, and download any result as CSV or
JSON. It doesn't change the database.

### 8.5 Option 3: the sqlite3 command line, inside a container

```powershell
docker run --rm -it -v "${PWD}:/data" -w /data alpine sh -c "apk add --no-cache sqlite && sqlite3 career_sifter.db"
```

This gives you the `sqlite>` prompt from section 4, option B (`.tables`,
`.headers on`, `.mode column`, queries, `.quit`), without installing sqlite3 on your
computer.

### Notes

- Don't run `fetch_jobs.py` while a container has the database open for writing. Browsing
  and queries are fine.
- On Windows, if the container can't see your files, open Docker Desktop → **Settings →
  Resources → File sharing** and make sure the drive or folder is shared.
- Port already in use? Change the first number, e.g. `-p 8090:8080`, and open
  http://localhost:8090.

## Troubleshooting

- **`python` is not recognised:** Python isn't installed or isn't on PATH. Redo step 1,
  then close and reopen PowerShell.
- **`sqlite3` is not recognised:** the command-line tool is optional (step 4, option B).
  After `winget install`, open a new PowerShell window.
- **"database is locked":** another program (usually DB Browser with unsaved changes)
  has the file open. Close it and rerun.
- **Start over:** delete `career_sifter.db`. The next run creates a fresh one.
