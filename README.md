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
