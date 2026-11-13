# Job Scraper API

FastAPI service that aggregates job postings from Indeed, LinkedIn, and configurable
company career pages.

> **Important:** Scraping Indeed and LinkedIn violates their Terms of Service, and both
> sites actively detect and block automated requests (CAPTCHAs, IP bans, JS-rendered
> content). The scrapers here are best-effort, for educational/personal/low-volume use,
> and **will need maintenance** as those sites change their markup. For production or
> commercial use, use Indeed's official Publisher/XML API and LinkedIn's official
> Talent/Jobs API (partner access).

## Project structure

```
job-scraper-api/
├── app/
│   ├── main.py                  # FastAPI app entrypoint
│   ├── api/
│   │   └── routes.py            # /api/v1/jobs/* endpoints
│   ├── core/
│   │   ├── config.py            # Settings (env-driven)
│   │   └── career_pages.json    # Config for generic career-page scraper
│   ├── models/
│   │   └── schemas.py           # Pydantic request/response models
│   ├── scrapers/
│   │   ├── base.py              # Abstract scraper interface
│   │   ├── indeed.py
│   │   ├── linkedin.py
│   │   └── career_pages.py      # Generic, selector-driven scraper
│   └── utils/
│       └── http_client.py       # Shared async HTTP client (retries, throttling)
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── README.md
```

## Run locally

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Visit http://localhost:8000/docs for interactive Swagger docs.

## Run with Docker

```bash
docker compose up --build
```

API available at http://localhost:8000, docs at http://localhost:8000/docs.

## Endpoints

| Method | Path                         | Description                                   |
|--------|-------------------------------|------------------------------------------------|
| GET    | `/health`                    | Health check                                   |
| GET    | `/api/v1/jobs/indeed`        | Search Indeed only                             |
| GET    | `/api/v1/jobs/linkedin`      | Search LinkedIn only                           |
| GET    | `/api/v1/jobs/career-pages`  | Search configured company career pages         |
| GET    | `/api/v1/jobs/search`        | Search all three sources concurrently          |

Query params: `query` (required), `location` (optional), `limit` (default 25, max 100).

Example:
```bash
curl "http://localhost:8000/api/v1/jobs/search?query=Python%20Developer&location=Remote&limit=10"
```

## Adding career pages

Edit `app/core/career_pages.json` — add an entry with the company name, URL, and CSS
selectors for the job list container, title, location, and link. No code changes needed.

## Configuration (env vars)

| Variable               | Default | Description                                  |
|-------------------------|---------|-----------------------------------------------|
| `DEBUG`                | false   | Verbose logging                                |
| `REQUEST_TIMEOUT`      | 15      | Outbound HTTP timeout (seconds)                |
| `MAX_RETRIES`          | 3       | Retry attempts per scrape request              |
| `MIN_REQUEST_INTERVAL` | 1.0     | Minimum seconds between outbound scrape calls  |
| `HTTP_PROXY`           | —       | Optional proxy URL to reduce IP blocking       |

## Known limitations

- Indeed/LinkedIn HTML structure changes frequently — CSS selectors in `scrapers/indeed.py`
  and `scrapers/linkedin.py` may need updates.
- Both sites may return CAPTCHA or empty/JS-only pages to non-browser traffic; for reliable
  results at scale, a headless browser (Playwright) and/or rotating proxies and residential
  IPs are typically required, which this project deliberately doesn't bundle.
- No persistence layer — results are returned per-request. Add a database (e.g. PostgreSQL)
  if you need to store/deduplicate listings over time.

## Scheduled database-backed job results

`GET /api/v1/jobs/scrape-all` now serves prepared MongoDB results through an API read cache. It returns
HTTP 200 with the existing `industries` and role/job fields. The worker collects
jobs independently, filters USA postings to 48 hours, selects at most 800 unique
URLs across all industries and sources, enriches companies, then publishes the
new result atomically. Selection rotates across industries, sources and roles.
Snapshot rows are written in small idempotent batches with retry handling.
MongoDB wire compression and HTTP gzip reduce transfer size for large results.
An individual job appears once in the response. `results_per_role_requested`
remains the collector's request size, while the final response has a global cap.

Run both services using the same MongoDB configuration:

```bash
docker compose up -d --build job-scraper-api job-scraper-worker
```

The worker checks for due work on startup and every 10 seconds. After a refresh
finishes, the next refresh is due in 30 minutes by default. A MongoDB lease with
a heartbeat prevents duplicate workers from refreshing simultaneously. Restarted
workers use the saved due time and can recover an abandoned lease. API workers
share the same published data. API requests never start scraping. Each API process checks the saved snapshot
in the background every 10 seconds and preloads changed data in small batches.
Requests use the completed cache while a new generation loads, and still filter
expired jobs at read time. The first API startup returns `ready: false` until
its initial cache load finishes. A database outage retains already-cached fresh
jobs and sets `refresh_error`. Process restarts reload the persistent result.

Before the first publication the response contains `ready: false` and no jobs.
Afterward, `ready: true` means a result has been published, even if no matching
jobs were available. `last_updated_at` records snapshot publication. Each entry
in `source_statuses` includes its adapter, status, collected/accepted counts,
last attempt, last successful sync and any error. `refresh_status` describes the
worker phase. A database connection failure returns HTTP 503.

An active refresh leaves the earlier result available. New and still-fresh saved
jobs are merged by canonical URL, including jobs from sources which failed in
the current cycle. Unknown publication dates, future dates, postings older than
48 hours and known-expired jobs are excluded. The API checks age again when
reading data. Source URLs are deduplicated after removing tracking parameters.
Different URLs for the same vacancy on different boards can still appear.

`GET /api/v1/jobs/unprocessed?category=IT` also reads saved jobs. Duplicate checks
and processed URLs now persist in MongoDB across API restarts.

Worker configuration in `.env`:

```dotenv
JOB_REFRESH_INTERVAL_SECONDS=1800
JOB_SOURCE_CONCURRENCY=6
JOB_SOURCE_TIMEOUT_SECONDS=180
JOB_DISCOVERY_LIMIT=30
USAJOBS_API_KEY=
USAJOBS_EMAIL=
ADZUNA_APP_ID=
ADZUNA_APP_KEY=
```

The API and worker must share `MONGODB_URL` and `MONGODB_DB_NAME`. Source execution
budgets start after a worker slot is available. Each source has one task per
refresh. Public-page discovery performs one board-wide search instead of one
search per role. A full refresh can take several minutes and its duration does
not block reads of the published data.

### Source coverage

`app/core/job_sources.json` contains the PDF's 61 sources. The worker additionally
reads the existing company career-page configuration.

- Indeed, LinkedIn, Glassdoor and ZipRecruiter use JobSpy. Glassdoor and
  ZipRecruiter require `HTTP_PROXY` to be configured.
- Remotive uses its public feed and reuses saved data for six hours between
  successful feed requests, respecting its lower polling frequency.
- USAJobs and Adzuna use official APIs and report `requires_configuration` until
  their keys are supplied.
- Dedicated adapters read Dice's public MCP job search, Remote OK, Working
  Nomads, We Work Remotely RSS, Himalayas, Ashby, Greenhouse, Amazon, Jibe,
  Oracle Recruiting, Workday, NYU's CWS feed and Microsoft's public career search.
  Configured public search pages also collect original JobPosting data from
  boards such as Built In, Wellfound, Jobspresso, Mayo and Kaiser.
- Remaining boards use bounded public-page discovery. Blocked or login-only
  sources cannot be guaranteed to return data. Check each source's status,
  collected/accepted/selected counts and rejection diagnostics. A successful
  fetch can still yield zero jobs inside the 48-hour window.
- Only original publication dates qualify. Provider update timestamps are not
  substituted for publication dates. Explicit US or worldwide eligibility is
  required. Verified ATS hosts are configured per source.
- `JOB_ADAPTER_MAX_PAGES` (default 5) and `JOB_ADAPTER_DETAIL_LIMIT` (default 100)
  bound collection work. Changing a source configuration invalidates its sync
  interval cache on the next scheduled refresh.
- Configured company career pages read embedded JobPosting data and optional
  CSS `date_selector` values. Unknown dates are excluded.

Catalogue inclusion does not establish successful live coverage of every board.
Inspect `source_statuses` to see which sources returned usable jobs. Provider
rate limits and access requirements can reduce the total below 800.

### Verification

The regression suite includes real MongoDB integration tests. Point
`JOB_TEST_MONGODB_URL` to an isolated test MongoDB and run:

```bash
JOB_TEST_MONGODB_URL=mongodb://localhost:27017 python -m unittest discover -s tests -v
```

Tests create and drop databases named `job_scheduler_test_<random-id>`. They
cover saved-result API reads, the 800-job cap, source/industry selection,
deduplication, expiry filtering, worker fencing, atomic publication, scheduling,
cancellation, authentication and persistent processed URLs.
