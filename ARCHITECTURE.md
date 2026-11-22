# Architecture

Diagrams of how Career Sifter is put together. They are written in
[Mermaid](https://mermaid.js.org/), which GitHub draws automatically. Edit the text to update
them.

1. [System overview](#1-system-overview)
2. [Finding the job board: entry paths and workarounds](#2-finding-the-job-board-entry-paths-and-workarounds)
3. [Collecting one company's jobs](#3-collecting-one-companys-jobs)
4. [Classes](#4-classes)
5. [Database tables](#5-database-tables)
6. [Tests](#6-tests)

---

## 1. System overview

Two scripts run one after the other. `find_careers.py` turns a list of websites into job-board
addresses. `fetch_jobs.py` reads the openings from those boards, tags them, and writes CSV/JSONL
files and the SQLite database.

```mermaid
%%{init: {"flowchart": {"htmlLabels": false}} }%%
flowchart LR
    input["sites.txt or sites.csv<br/>homepages, careers<br/>pages or board URLs"]
    known[("known_sites.csv<br/>saved boards and<br/>manual-check notes")]
    fc["find_careers.py<br/>SiteScanner"]
    results["careers_results.csv<br/>one row per company"]
    fj["fetch_jobs.py"]
    clients["ats_clients.py<br/>11 job-board<br/>adapters"]
    tag["classify.py<br/>job_details.py"]
    out["output folder<br/>summary.csv<br/>us_locations.csv<br/>jobs.csv, jobs.jsonl"]
    db[("career_sifter.db<br/>SQLite")]
    web(("company sites<br/>and job boards"))
    tools["DB Browser, sqlite3,<br/>R, pandas, Docker"]

    input --> fc
    known <--> fc
    fc <-->|"HTTP, robots.txt<br/>respected, refusals<br/>recorded"| web
    fc --> results --> fj
    fj --> clients
    clients <-->|"job-board APIs<br/>and pages"| web
    fj --> tag
    fj --> out
    fj -->|"database.py:<br/>create or add to"| db
    db --> tools
```

## 2. Finding the job board: entry paths and workarounds

What `SiteScanner.scan()` does for one input row. Every path ends in a status
(`listings_found`, `careers_page_found`, `not_found`, `blocked`, `error`), a `manual_check`
note when a person needs to look, and an update to `known_sites.csv`.

```mermaid
%%{init: {"flowchart": {"htmlLabels": false}} }%%
flowchart TD
    start(["input row"]) --> saved{"saved entry in<br/>known_sites.csv?"}
    saved -->|"no board, checked<br/>within 30 days"| skip["skip: 0 requests,<br/>repeat the<br/>manual-check note"]
    saved -->|"has board or<br/>careers page"| useSaved["start from the<br/>saved board or<br/>careers page"]
    saved -->|"no, or --refresh"| kind{"what kind<br/>of URL?"}
    useSaved -->|works| done
    useSaved -->|"404 or failed"| kind

    kind -->|"job board"| board["use it as-is,<br/>no crawling"]
    kind -->|"careers page"| careers["fetch the careers<br/>page; homepage is<br/>never requested"]
    kind -->|homepage| home["fetch the homepage<br/>(also www. and<br/>http://)"]

    careers -->|"refused or failed"| search["workaround:<br/>site search pages<br/>/us/en/search-results,<br/>/en/jobs, ...<br/>stop after 3 refusals"]
    search -->|loads| analyse
    search -->|"none load"| probe
    careers -->|loads| analyse

    home -->|loads| links["score every link:<br/>careers, jobs, join us,<br/>job-board hosts"]
    home -->|"refused, robots.txt<br/>or server error"| sub["workaround:<br/>careers.domain or<br/>jobs.domain, then<br/>their search pages"]
    sub -->|loads| analyse
    sub -->|"none load"| probe
    links -->|"no good link"| paths["probe /careers,<br/>/jobs, ... then<br/>the sitemap"]
    links --> verify["fetch the top 3<br/>candidates and<br/>score their content"]
    paths --> verify
    verify --> analyse["analyse the page:<br/>JobPosting data,<br/>job-board links,<br/>listing phrases"]
    analyse --> listings["follow to listings:<br/>one more hop if<br/>needed; skip refused,<br/>login, internal links"]
    listings -->|"ATS found"| done(["listings_found"])
    listings -->|"no ATS"| probe

    probe["workaround: guess<br/>board addresses:<br/>Workday data centre<br/>and site names; iCIMS,<br/>Greenhouse, Lever,<br/>Ashby, SmartRecruiters"]
    probe -->|found| done
    probe -->|nothing| manual(["careers_page_found,<br/>not_found or blocked,<br/>plus a manual-check<br/>note: open the page,<br/>paste the board into<br/>known_sites.csv"])
    board --> done
```

## 3. Collecting one company's jobs

What `fetch_jobs.process_company()` does for one row of `careers_results.csv`. Companies run in
parallel (`--workers`); within a company, job pages are fetched in parallel (`--detail-workers`).

```mermaid
sequenceDiagram
    autonumber
    participant FJ as fetch_jobs.py
    participant C as ATS client
    participant B as job board
    participant T as classify.py + job_details.py
    participant DB as database.py

    FJ->>C: create the client for the row's ATS
    C->>B: robots.txt (cached per host)
    alt robots.txt disallows
        C-->>FJ: RobotsDisallowed
        Note over FJ: status robots_disallowed, manual_check note
    else allowed
        FJ->>C: list_jobs(us_only)
        C->>B: listing API / pages (Workday uses its own US filter)
        B-->>C: openings with title, location, maybe salary
        C-->>FJ: Job records + total_openings
        Note over FJ: keep US jobs and unknown locations
        par each job without a full description
            FJ->>C: enrich(job)
            C->>B: job page / detail API
            B-->>C: description, locations, schema.org data
        end
        FJ->>C: finalize(jobs)
        Note over C: Workday: state facets for bare site names
        Note over FJ: infer_missing_states: other postings, then "city, ST" in the text
        loop each US job
            FJ->>T: classify_job
            T-->>FJ: education, years, career level, keywords,<br/>salary, arrangement, term, shifts, travel,<br/>visa, relocation, bonus, equity, certifications
        end
    end
    FJ->>FJ: write_outputs: CSV and JSONL
    FJ->>DB: save_run: upsert jobs, snapshot,<br/>close jobs no longer listed (complete runs only)
```

## 4. Classes

The main classes and how they relate. `SiteScanner` (in `find_careers.py`) produces a `Result`
per input row. Each `ATSClient` subclass turns one kind of job board into `Job` records, which
`fetch_jobs.py` groups into a `CompanyReport`.

### 4a. Finding the board (`find_careers.py`)

```mermaid
classDiagram
    direction LR

    class SiteScanner {
        +Result result
        +dict known
        +int requests
        +scan() Result
        scan_from_known()
        scan_from_careers_page(url)
        scan_from_homepage()
        try_search_pages(origin) Page
        find_listings(page, sig)
        probe_ats_boards()
        probe_workday(slug)
    }
    class Result {
        input_url
        input_type
        source
        status
        careers_url
        listings_url
        ats
        manual_check
        rejected_requests
        requests
    }
    class Page {
        url
        status
        html
    }
    class PageSignals {
        score
        ats
        ats_url
        jobposting
    }

    SiteScanner --> Result : fills
    SiteScanner ..> Page : fetches
    SiteScanner ..> PageSignals : analyze_page
```

### 4b. Collecting jobs (`ats_clients.py`, `fetch_jobs.py`)

```mermaid
classDiagram
    direction TB

    class ATSClient {
        <<abstract>>
        +total_openings
        +us_filtered
        +rejections
        +list_jobs(us_only) list~Job~
        +enrich(job)
        +finalize(jobs)
        -_check_robots(url)
    }
    class Job {
        company
        ats
        job_id
        title
        url
        locations
        states
        is_us
        description
        career_level
        min_education
        salary_min
        salary_max
        salary_annual_mid
        work_arrangement
        employment_term
        certifications
    }
    class CompanyReport {
        company
        company_key
        status
        total_openings
        us_openings
        jobs
        manual_check
    }

    ATSClient <|-- WorkdayClient
    ATSClient <|-- GreenhouseClient
    ATSClient <|-- LeverClient
    ATSClient <|-- AshbyClient
    ATSClient <|-- SmartRecruitersClient
    ATSClient <|-- EightfoldClient
    ATSClient <|-- PhenomClient
    ATSClient <|-- ICIMSClient
    ATSClient <|-- AttraxClient
    ATSClient <|-- OracleClient
    ATSClient <|-- PeopleAdminClient
    ATSClient ..> Job : creates
    CompanyReport o-- Job : US jobs
```

How each adapter reads its board:

| Adapter | Reads | Notes |
|---|---|---|
| Workday | `/wday/cxs/.../jobs` JSON | built-in US filter; state facets for bare site names |
| Greenhouse, Lever, Ashby | public board APIs | Lever/Ashby pay fields used directly |
| SmartRecruiters | public postings API | its robots.txt disallows the API, so it is skipped by default |
| Eightfold | `/api/pcsx` | its robots.txt disallows the API, so it is skipped by default |
| Phenom | JSON embedded in search pages (`phApp.ddo`) | career site on the company's domain |
| iCIMS | classic portal pages + schema.org, or career-site `/api/jobs` | follows portal → career-site redirects |
| Attrax | `/<lang>/jobs` tiles + schema.org | site's own level, salary, workplace type |
| Oracle Recruiting Cloud | `recruitingCE` REST API | vanity domains detected by path |
| PeopleAdmin | `/postings/search.atom` + field tables | its default robots.txt disallows scripts |

## 5. Database tables

`career_sifter.db`, created on the first run and added to afterwards. `jobs` has every column of
`jobs.csv` plus the description and tracking columns (only the main ones are shown). Views:
`v_open_jobs` (`is_open = 1`) and `v_latest_snapshots`.

```mermaid
erDiagram
    runs ||--o{ company_snapshots : records
    companies ||--o{ company_snapshots : "state per run"
    companies ||--o{ jobs : lists
    jobs ||--o{ job_states : "located in"
    jobs ||--o{ job_education : mentions
    jobs ||--o{ job_certifications : requires
    jobs ||--o{ job_keywords : matches

    runs {
        int run_id PK
        text started_at
        text input_file
        int complete
        int us_only
        int n_jobs
    }
    companies {
        text company_key PK
        text name
        text ats
        text board_url
        text first_seen
        text last_seen
    }
    company_snapshots {
        int run_id PK
        text company_key PK
        text status
        int total_openings
        int us_openings
        text manual_check
    }
    jobs {
        text job_uid PK
        text company_key FK
        text title
        text url
        text career_level
        text min_education
        real salary_annual_mid
        text work_arrangement
        text description
        int first_seen_run
        int last_seen_run
        int is_open
        text closed_at
    }
    job_states {
        text job_uid FK
        text state
    }
    job_education {
        text job_uid FK
        text level
    }
    job_certifications {
        text job_uid FK
        text certification
    }
    job_keywords {
        text job_uid FK
        text keyword
    }
```

## 6. Tests

`python test.py` checks the installation, then runs every `test_*.py` file. It builds
`tests/fixture.db`, which the R tests use for exact answers. `--live` and `--r` add the
internet and R checks.

```mermaid
%%{init: {"flowchart": {"htmlLabels": false}} }%%
flowchart LR
    t["test.py"]
    i["installation checks:<br/>Python, packages,<br/>SQLite, modules,<br/>write access"]
    py["test_classify.py<br/>test_job_details.py<br/>test_find_careers.py<br/>test_pipeline.py<br/>test_database.py"]
    fx[("tests/fixture.db<br/>known contents")]
    live["--live<br/>benchling.com,<br/>both scripts"]
    r["--r<br/>tests/run_r_tests.R"]
    rt["tests/testthat/<br/>test-database.R"]
    real[("career_sifter.db")]

    t --> i --> py
    py -->|"test_database.py builds"| fx
    t -.-> live
    t -.-> r --> rt
    rt -->|"exact answers"| fx
    rt -.->|"data quality and<br/>tutorial queries"| real
```
