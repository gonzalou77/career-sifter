# Tests for using the Career Sifter database from R (DATABASE.md section 7).
# Run with: Rscript tests/run_r_tests.R [database]   (the runner sets CAREER_SIFTER_DB)

# quiet loading: "package was built under R version x.y.z" warnings aren't test results
load_quietly <- function(...) for (p in c(...)) suppressWarnings(suppressPackageStartupMessages(
  library(p, character.only = TRUE)))
load_quietly("DBI")

db_path <- Sys.getenv("CAREER_SIFTER_DB", file.path("..", "fixture.db"))
con <- dbConnect(RSQLite::SQLite(), db_path)
withr::defer(dbDisconnect(con), teardown_env())

q <- function(sql, ...) dbGetQuery(con, sql, ...)
# the fixture is built by test_database.py's build_fixture(); its runs are labelled "fixture"
is_fixture <- "fixture" %in% q("SELECT input_file FROM runs")$input_file

acme_key <- "board:acme.wd1.myworkdayjobs.com/external"  # the fixture's pasted job board
us_states <- c(state.abb, "DC", "PR")
career_levels <- c("early", "mid", "senior", "executive", "unspecified")

# --------------------------------------------------------------------------- #
# structure: holds for any database made by fetch_jobs.py
# --------------------------------------------------------------------------- #

test_that("the database has the expected tables and views", {
  expect_true(all(c("runs", "companies", "company_snapshots", "jobs", "job_states", "job_education",
                    "job_certifications", "job_keywords", "v_open_jobs", "v_latest_snapshots")
                  %in% dbListTables(con)))
})

test_that("jobs has the columns the tutorial uses", {
  expect_true(all(c("job_uid", "company_key", "company", "title", "url", "career_level", "min_education",
                    "salary_min", "salary_max", "salary_period", "salary_annual_mid", "work_arrangement",
                    "states", "first_seen_at", "last_seen_at", "first_seen_run", "last_seen_run",
                    "is_open", "closed_at") %in% dbListFields(con, "jobs")))
})

# --------------------------------------------------------------------------- #
# data quality: holds for any database
# --------------------------------------------------------------------------- #

test_that("each job appears once", {
  n <- q("SELECT COUNT(*) AS n, COUNT(DISTINCT job_uid) AS distinct_n FROM jobs")
  expect_equal(n$n, n$distinct_n)
})

test_that("salary values are consistent", {
  expect_equal(q("SELECT COUNT(*) AS n FROM jobs WHERE salary_min > salary_max")$n, 0)
  expect_equal(q("SELECT COUNT(*) AS n FROM jobs WHERE salary_annual_mid <= 0")$n, 0)
  periods <- q("SELECT DISTINCT salary_period FROM jobs WHERE salary_period != ''")$salary_period
  expect_true(all(periods %in% c("hour", "week", "month", "year")))
})

test_that("open/closed tracking is consistent", {
  expect_true(all(q("SELECT DISTINCT is_open FROM jobs")$is_open %in% c(0, 1)))
  expect_equal(q("SELECT COUNT(*) AS n FROM jobs WHERE is_open = 0 AND closed_at IS NULL")$n, 0)
  expect_equal(q("SELECT COUNT(*) AS n FROM jobs WHERE is_open = 1 AND closed_at IS NOT NULL")$n, 0)
  expect_equal(q("SELECT COUNT(*) AS n FROM jobs WHERE first_seen_run > last_seen_run")$n, 0)
})

test_that("open jobs have a link", {
  expect_equal(q("SELECT COUNT(*) AS n FROM v_open_jobs WHERE url IS NULL OR url = ''")$n, 0)
})

test_that("states are valid US codes and no detail rows are orphaned", {
  expect_true(all(q("SELECT DISTINCT state FROM job_states")$state %in% us_states))
  for (tbl in c("job_states", "job_education", "job_certifications", "job_keywords")) {
    orphans <- q(sprintf("SELECT COUNT(*) AS n FROM %s t LEFT JOIN jobs j USING (job_uid)
                          WHERE j.job_uid IS NULL", tbl))$n
    expect_equal(orphans, 0, label = paste("orphaned rows in", tbl))
  }
})

test_that("career levels use the documented values", {
  lv <- q("SELECT DISTINCT career_level FROM jobs WHERE career_level != ''")$career_level
  expect_true(all(lv %in% career_levels))
})

test_that("no company is keyed by a job-board platform's own domain", {
  # a pasted board is keyed "board:<host>/<path>"; a key like myworkdayjobs.com means several
  # companies were merged into one (fixed in fetch_jobs.py - rerun to repair an older database)
  ats_domains <- c("myworkdayjobs.com", "myworkdaysite.com", "greenhouse.io", "lever.co", "ashbyhq.com",
                   "smartrecruiters.com", "icims.com", "eightfold.ai", "oraclecloud.com", "taleo.net",
                   "successfactors.com", "jobvite.com", "peopleadmin.com")
  keys <- q("SELECT company_key FROM companies")$company_key
  expect_false(any(keys %in% ats_domains), label = paste("ATS-domain keys:", toString(keys[keys %in% ats_domains])))
  expect_false(any(is.na(keys) | keys == ""))
})

test_that("every row points at an existing company and run", {
  expect_equal(q("SELECT COUNT(*) AS n FROM jobs j LEFT JOIN companies c USING (company_key)
                  WHERE c.company_key IS NULL")$n, 0)
  expect_equal(q("SELECT COUNT(*) AS n FROM company_snapshots s LEFT JOIN runs r USING (run_id)
                  WHERE r.run_id IS NULL")$n, 0)
  expect_equal(q("SELECT COUNT(*) AS n FROM company_snapshots s LEFT JOIN companies c USING (company_key)
                  WHERE c.company_key IS NULL")$n, 0)
  # job_uid is company_key|platform|job id, so the same job id at two companies stays two jobs
  expect_equal(q("SELECT COUNT(*) AS n FROM jobs WHERE substr(job_uid, 1, length(company_key) + 1) != company_key || '|'")$n, 0)
})

test_that("run numbers and dates are in order", {
  max_run <- q("SELECT MAX(run_id) AS m FROM runs")$m
  expect_equal(q("SELECT COUNT(*) AS n FROM jobs WHERE last_seen_run > ?", params = list(max_run))$n, 0)
  expect_equal(q("SELECT COUNT(*) AS n FROM jobs WHERE closed_at < first_seen_at OR last_seen_at < first_seen_at")$n, 0)
  expect_equal(q("SELECT COUNT(*) AS n FROM runs WHERE finished_at < started_at")$n, 0)
})

test_that("the states column agrees with the job_states table", {
  jobs <- q("SELECT job_uid, states FROM jobs")
  rows <- q("SELECT job_uid, state FROM job_states ORDER BY job_uid, state")
  from_column <- lapply(strsplit(ifelse(is.na(jobs$states), "", jobs$states), "; ", fixed = TRUE), sort)
  names(from_column) <- jobs$job_uid
  from_table <- split(rows$state, rows$job_uid)
  for (uid in jobs$job_uid) {
    expect_equal(from_column[[uid]], if (is.null(from_table[[uid]])) character(0) else from_table[[uid]],
                 label = paste("states of", uid))
  }
})

test_that("annual pay midpoint matches the range and period", {
  per_year <- c(hour = 2080, week = 52, month = 12, year = 1)
  pay <- q("SELECT salary_min, salary_max, salary_period, salary_annual_mid FROM jobs
            WHERE salary_annual_mid IS NOT NULL")
  if (nrow(pay)) {
    f <- unname(per_year[pay$salary_period])
    expect_false(any(is.na(f)), label = "pay with an unknown period")
    expect_true(all(pay$salary_annual_mid >= pay$salary_min * f - 1 & pay$salary_annual_mid <= pay$salary_max * f + 1))
    expect_equal(pay$salary_annual_mid, round((pay$salary_min + pay$salary_max) / 2 * f), tolerance = 1e-6)
  }
  currencies <- q("SELECT DISTINCT salary_currency FROM jobs WHERE salary_currency != ''")$salary_currency
  expect_true(all(currencies %in% c("USD", "CAD", "EUR", "GBP")))
})

# --------------------------------------------------------------------------- #
# the tutorial code (DATABASE.md sections 5 and 7) runs on this database
# --------------------------------------------------------------------------- #

test_that("tutorial SQL queries run and return the documented columns", {
  expect_named(q("SELECT company_key, COUNT(*) AS open_jobs FROM v_open_jobs
                  GROUP BY company_key ORDER BY open_jobs DESC"), c("company_key", "open_jobs"))
  expect_named(q("SELECT s.state, COUNT(*) AS jobs FROM job_states s JOIN v_open_jobs j USING (job_uid)
                  GROUP BY s.state ORDER BY jobs DESC LIMIT 10"), c("state", "jobs"))
  expect_named(q("SELECT career_level, COUNT(*) AS jobs_with_pay, ROUND(AVG(salary_annual_mid)) AS avg_annual_mid
                  FROM v_open_jobs WHERE salary_annual_mid IS NOT NULL GROUP BY career_level"),
               c("career_level", "jobs_with_pay", "avg_annual_mid"))
  expect_named(q("SELECT company_key, COUNT(*) AS new_jobs FROM jobs
                  WHERE first_seen_run = (SELECT MAX(run_id) FROM runs) GROUP BY company_key"),
               c("company_key", "new_jobs"))
  expect_named(q("SELECT company_key, title, ROUND(julianday(closed_at) - julianday(first_seen_at)) AS days_listed
                  FROM jobs WHERE is_open = 0"), c("company_key", "title", "days_listed"))
  expect_s3_class(q("SELECT title FROM v_open_jobs WHERE company_key = ?", params = list("lilly.com")), "data.frame")
})

test_that("tables load as data frames and dates convert", {
  jobs <- dbReadTable(con, "v_open_jobs")
  expect_s3_class(jobs, "data.frame")
  expect_equal(nrow(jobs), q("SELECT COUNT(*) AS n FROM v_open_jobs")$n)
  first_seen <- as.Date(substr(jobs$first_seen_at, 1, 10))
  expect_false(any(is.na(first_seen)))
})

test_that("dplyr (dbplyr) gives the same answer as SQL", {
  load_quietly("dplyr", "dbplyr")
  via_dplyr <- tbl(con, "v_open_jobs") |>
    filter(!is.na(salary_annual_mid)) |>
    group_by(career_level) |>
    summarise(jobs = n(), avg_pay = mean(salary_annual_mid, na.rm = TRUE)) |>
    arrange(career_level) |>
    collect()
  via_sql <- q("SELECT career_level, COUNT(*) AS jobs, AVG(salary_annual_mid) AS avg_pay FROM v_open_jobs
                WHERE salary_annual_mid IS NOT NULL GROUP BY career_level ORDER BY career_level")
  expect_equal(as.data.frame(via_dplyr), via_sql, ignore_attr = TRUE)
})

test_that("the tutorial chart builds", {
  skip_if_not_installed("ggplot2")
  load_quietly("dplyr", "ggplot2")
  pay <- tbl(con, "v_open_jobs") |>
    filter(!is.na(salary_annual_mid)) |>
    select(company_key, career_level, salary_annual_mid) |>
    collect()
  skip_if(nrow(pay) == 0, "no jobs with pay in this database")
  pay$career_level <- factor(pay$career_level, levels = career_levels)
  out <- tempfile(fileext = ".png")
  p <- ggplot(pay, aes(x = career_level, y = salary_annual_mid)) + geom_boxplot() +
    scale_y_continuous(labels = scales::dollar)
  ggsave(out, p, width = 7, height = 4)
  expect_true(file.exists(out))
})

# --------------------------------------------------------------------------- #
# exact answers: only on tests/fixture.db (known contents, see test_database.py)
# --------------------------------------------------------------------------- #

test_that("fixture: runs, companies and open/closed counts", {
  skip_if_not(is_fixture, "not the fixture database")
  expect_equal(q("SELECT COUNT(*) AS n FROM runs")$n, 2)
  expect_equal(q("SELECT COUNT(*) AS n FROM companies")$n, 3)
  expect_equal(q("SELECT COUNT(*) AS n FROM jobs")$n, 7)
  expect_equal(q("SELECT COUNT(*) AS n FROM v_open_jobs")$n, 6)
  r3 <- q("SELECT is_open, closed_at FROM jobs WHERE job_id = 'R3'")
  expect_equal(r3$is_open, 0)
  expect_false(is.na(r3$closed_at))
})

test_that("fixture: open jobs per company and top states", {
  skip_if_not(is_fixture, "not the fixture database")
  per_company <- q("SELECT company_key, COUNT(*) AS open_jobs FROM v_open_jobs GROUP BY company_key ORDER BY company_key")
  expect_equal(per_company$company_key, c(acme_key, "lilly.com", "pfizer.com"))
  expect_equal(per_company$open_jobs, c(1, 3, 2))
  states <- q("SELECT s.state, COUNT(*) AS jobs FROM job_states s JOIN v_open_jobs j USING (job_uid)
               GROUP BY s.state ORDER BY s.state")
  expect_equal(setNames(states$jobs, states$state), c(IN = 2, NJ = 1, NY = 2, OH = 1, TX = 1))
})

test_that("fixture: pay by career level and education", {
  skip_if_not(is_fixture, "not the fixture database")
  pay <- q("SELECT career_level, COUNT(*) AS n, AVG(salary_annual_mid) AS avg_pay FROM v_open_jobs
            WHERE salary_annual_mid IS NOT NULL GROUP BY career_level ORDER BY avg_pay")
  expect_equal(pay$career_level, c("mid", "senior", "executive"))
  expect_equal(pay$n, c(1, 2, 1))
  expect_equal(pay$avg_pay, c(95000, 152500, 350000))
  edu <- q("SELECT min_education, COUNT(*) AS n FROM v_open_jobs GROUP BY min_education ORDER BY min_education")
  expect_equal(setNames(edu$n, edu$min_education), c("Bachelor's" = 3, PhD = 3))
  phd_pay <- q("SELECT j.job_id FROM v_open_jobs j JOIN job_education e USING (job_uid)
                WHERE e.level = 'PhD' AND j.salary_min IS NOT NULL ORDER BY j.salary_max DESC")
  expect_equal(phd_pay$job_id, c("P2", "R2"))
})

test_that("fixture: a job updated across runs keeps its first-seen run", {
  skip_if_not(is_fixture, "not the fixture database")
  r1 <- q("SELECT title, first_seen_run, last_seen_run, salary_annual_mid FROM jobs
           WHERE job_id = 'R1' AND company_key = 'lilly.com'")
  expect_equal(nrow(r1), 1)
  expect_equal(r1$title, "Scientist II")
  expect_equal(c(r1$first_seen_run, r1$last_seen_run), c(1, 2))
  expect_equal(r1$salary_annual_mid, 95000)
  new_jobs <- q("SELECT job_id FROM jobs WHERE first_seen_run = (SELECT MAX(run_id) FROM runs)")
  expect_equal(new_jobs$job_id, "R4")
  snaps <- q("SELECT run_id, us_openings, total_openings FROM company_snapshots
              WHERE company_key = 'lilly.com' ORDER BY run_id")
  expect_equal(snaps$total_openings, c(641, 642))
})

test_that("fixture: the same job id at two companies is two jobs", {
  skip_if_not(is_fixture, "not the fixture database")
  r1 <- q("SELECT company_key, title FROM jobs WHERE job_id = 'R1' ORDER BY company_key")
  expect_equal(r1$company_key, c(acme_key, "lilly.com"))
  expect_equal(r1$title, c("Lab Technician", "Scientist II"))
})

test_that("fixture: a blocked board and a cut-short listing close nothing", {
  skip_if_not(is_fixture, "not the fixture database")
  # Acme (pasted board) was blocked on run 2; Pfizer's run-2 listing was cut short and missed P1
  kept <- q("SELECT company_key, job_id, is_open, last_seen_run, closed_at FROM jobs
             WHERE job_id = 'P1' OR company_key = ? ORDER BY job_id", params = list(acme_key))
  expect_equal(kept$job_id, c("P1", "R1"))
  expect_equal(kept$is_open, c(1, 1))
  expect_equal(kept$last_seen_run, c(1, 1))
  expect_true(all(is.na(kept$closed_at)))
  acme <- q("SELECT run_id, status, total_openings FROM company_snapshots WHERE company_key = ? ORDER BY run_id",
            params = list(acme_key))
  expect_equal(acme$status, c("ok", "blocked"))
  expect_equal(acme$total_openings, c(1, NA))
  # only the job a complete, successful listing no longer showed was closed
  expect_equal(q("SELECT job_id FROM jobs WHERE is_open = 0")$job_id, "R3")
})
