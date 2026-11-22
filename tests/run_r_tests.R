# Run the R tests for the Career Sifter database.
#
#   Rscript tests/run_r_tests.R                      # against tests/fixture.db (built by `python test.py`)
#   Rscript tests/run_r_tests.R career_sifter.db     # against your real database
#
# With the fixture, exact answers are checked as well. With any other database, the checks that hold
# for any data run: tables, data quality, tutorial queries, dplyr vs SQL, the tutorial chart.

script_arg <- grep("^--file=", commandArgs(trailingOnly = FALSE), value = TRUE)
tests_dir <- if (length(script_arg)) dirname(normalizePath(sub("^--file=", "", script_arg))) else "tests"

args <- commandArgs(trailingOnly = TRUE)
db <- if (length(args) >= 1) args[1] else file.path(tests_dir, "fixture.db")
if (!file.exists(db)) {
  stop("database not found: ", db,
       "\nRun `python test.py` once to build tests/fixture.db, or pass the path to a database.", call. = FALSE)
}

needed <- c("testthat", "DBI", "RSQLite", "dplyr", "dbplyr")
missing <- needed[!vapply(needed, function(p) suppressWarnings(requireNamespace(p, quietly = TRUE)), logical(1))]
if (length(missing)) {
  stop("missing R packages: ", paste(missing, collapse = ", "),
       "\nInstall them in R with: install.packages(c(", paste0('"', missing, '"', collapse = ", "), "))",
       call. = FALSE)
}

Sys.setenv(CAREER_SIFTER_DB = normalizePath(db))
cat("Testing database:", normalizePath(db), "\n")
suppressWarnings(  # "package was built under R version x.y.z" is not a test result
  testthat::test_dir(file.path(tests_dir, "testthat"), reporter = "summary", stop_on_failure = TRUE))
cat("all R database checks passed\n")
