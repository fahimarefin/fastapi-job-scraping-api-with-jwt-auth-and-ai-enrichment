#app/api/routes.py
import asyncio
import logging
from collections import defaultdict

import httpx
from fastapi import APIRouter, HTTPException, Query

from app.models.schemas import (
    ALL_ROLES_BY_INDUSTRY,
    CATEGORY_SLUG_MAP,
    SCRAPE_LIMIT,
    SCRAPE_LOCATION,
    CategoriesResponse,
    CategoryInfo,
    DuplicateCheckRequest,
    DuplicateCheckResponse,
    JobListing,
    JobSource,
    MarkProcessedRequest,
    MarkProcessedResponse,
    RoleResult,
    ScrapeAllResponse,
    UnprocessedJobsResponse,
)
from app.scrapers.career_pages import CareerPageScraper
from app.scrapers.enrichment_service import enricher
from app.scrapers.jobspy_scraper import JobSpyScraper

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])
categories_router = APIRouter(prefix="/api/v1", tags=["categories"])
trigger_router = APIRouter(prefix="/api/v1", tags=["trigger"])

N8N_WEBHOOK = "https://n8n.srv1795032.hstgr.cloud/webhook-test/recruitment/scrape"

# ---------------------------------------------------------------------------
# In-memory processed-URL store
# Tracks job URLs that have already been handled by downstream consumers.
# Wiped on server restart — upgrade to Redis/SQLite for persistence.
# ---------------------------------------------------------------------------
_processed_urls: set[str] = set()

_jobspy = JobSpyScraper(sites=["indeed", "linkedin", "glassdoor", "zip_recruiter"])
_career_page_scraper = CareerPageScraper()




async def _scrape_role(
    role: str,
    industry: str,
    limit: int = SCRAPE_LIMIT,
    hours_old: int | None = 48,
) -> RoleResult:
    """
    Run JobSpy and CareerPageScraper for one role at the same time.
    Filtered by hours_old (e.g. 48 hours for 1-2 days old fresh jobs).
    """
    errors: dict[str, str] = {}

    # Launch both scrapers simultaneously
    jobspy_task = asyncio.ensure_future(_jobspy.search(role, SCRAPE_LOCATION, limit, hours_old=hours_old))
    career_task = asyncio.ensure_future(_career_page_scraper.search(role, SCRAPE_LOCATION, limit, hours_old=hours_old))

    jobspy_listings: list[JobListing] = []
    career_listings: list[JobListing] = []

    jobspy_result, career_result = await asyncio.gather(
        jobspy_task, career_task, return_exceptions=True
    )

    if isinstance(jobspy_result, Exception):
        logger.exception("JobSpy failed for role '%s': %s", role, jobspy_result)
        for site in _jobspy.active_sites:
            errors[site] = str(jobspy_result)
    else:
        jobspy_listings = jobspy_result[:limit]  # type: ignore[index]

    if isinstance(career_result, Exception):
        logger.exception("CareerPage scraper failed for role '%s': %s", role, career_result)
        errors[JobSource.CAREER_PAGE.value] = str(career_result)
    else:
        career_listings = career_result[:limit]  # type: ignore[index]

    jobs = jobspy_listings + career_listings

    return RoleResult(
        role=role,
        industry=industry,
        location=SCRAPE_LOCATION,
        total=len(jobs),
        jobs=jobs,
        errors=errors,
    )


@router.get(
    "/scrape-all",
    response_model=ScrapeAllResponse,
    summary="Scrape all roles (no parameters needed)",
    description=(
        "Automatically scrapes fresh jobs (1–2 days old, 10 results per role, ~800 total) for **every hardcoded role** "
        "across all 10 industries, resolves the hiring company, finds HR and executive "
        "decision-makers, and retrieves their LinkedIn profiles and business emails.\n\n"
        "- **Location**: USA (fixed)\n"
        "- **Limit**: 10 results per role per source (fixed, ~800 total)\n"
        "- **Freshness**: 1–2 days old (48 hours)\n"
        "- **Decision Makers**: HR & Executives with LinkedIn & email automatically enriched\n"
        "- **Roles**: fully hardcoded — no input required\n\n"
        "> ⚠️ This endpoint takes ~1-2 minutes due to scraping across 10 industries."
    ),
)
async def scrape_all() -> ScrapeAllResponse:
    hours_old = 48  # Fresh, warm jobs — postings 1–2 days old

    role_to_industries: dict[str, list[str]] = defaultdict(list)
    for industry, roles in ALL_ROLES_BY_INDUSTRY.items():
        for role in roles:
            role_to_industries[role].append(industry)

    unique_roles = list(role_to_industries.keys())
    logger.info(
        "scrape_all: %d unique roles to scrape (from %d total role-industry pairs)",
        len(unique_roles),
        sum(len(v) for v in ALL_ROLES_BY_INDUSTRY.values()),
    )

    # Semaphore: cap peak concurrent role tasks
    MAX_CONCURRENT_ROLES = 20
    sem = asyncio.Semaphore(MAX_CONCURRENT_ROLES)

    async def _scrape_unique_role(role: str) -> RoleResult:
        async with sem:
            # Use the first industry as the canonical one for the scrape call
            primary_industry = role_to_industries[role][0]
            return await _scrape_role(role, primary_industry, limit=SCRAPE_LIMIT, hours_old=hours_old)

    # Fire all unique roles concurrently
    all_results: list[RoleResult] = await asyncio.gather(
        *[_scrape_unique_role(role) for role in unique_roles]
    )

    # Collect all scraped listings across all roles
    all_listings: list[JobListing] = []
    for r in all_results:
        all_listings.extend(r.jobs)

    # Automatically enrich companies & decision-makers with LinkedIn and email
    if all_listings:
        logger.info("Enriching %d listings with decision-maker contacts...", len(all_listings))
        await enricher.enrich_listings(all_listings, max_contacts=2)

    # Map each result to ALL industries that include this role
    industries_result: dict[str, list[RoleResult]] = defaultdict(list)
    total_jobs = 0

    for result in all_results:
        role = result.role
        total_jobs += result.total
        logger.info("  '%s' → %d jobs", role, result.total)

        for industry in role_to_industries[role]:
            # If this role belongs to multiple industries, clone the result
            industries_result[industry].append(
                result if industry == role_to_industries[role][0]
                else RoleResult(
                    role=role,
                    industry=industry,
                    location=result.location,
                    total=result.total,
                    jobs=list(result.jobs),
                    errors=dict(result.errors),
                )
            )

    total_roles = sum(len(v) for v in industries_result.values())

    return ScrapeAllResponse(
        location=SCRAPE_LOCATION,
        results_per_role_requested=SCRAPE_LIMIT,
        total_roles_scraped=total_roles,
        total_jobs_found=total_jobs,
        industries=dict(industries_result),
    )




# ---------------------------------------------------------------------------
# GET /api/v1/categories
# ---------------------------------------------------------------------------

@categories_router.get(
    "/categories",
    response_model=CategoriesResponse,
    summary="List all job categories",
    description=(
        "Returns every available category (industry) with its URL-safe slug, "
        "display name, and the list of roles that will be scraped for it.\n\n"
        "Use the `slug` value as the `category` query parameter for "
        "`GET /api/v1/jobs/unprocessed`."
    ),
)
async def get_categories() -> CategoriesResponse:
    """Return all categories derived from CATEGORY_SLUG_MAP and ALL_ROLES_BY_INDUSTRY."""
    categories: list[CategoryInfo] = []
    for slug, industry_name in CATEGORY_SLUG_MAP.items():
        roles = ALL_ROLES_BY_INDUSTRY.get(industry_name, [])
        categories.append(
            CategoryInfo(
                slug=slug,
                name=industry_name,
                role_count=len(roles),
                roles=roles,
            )
        )
    return CategoriesResponse(total=len(categories), categories=categories)


# ---------------------------------------------------------------------------
# POST /api/v1/jobs/check-duplicates
# ---------------------------------------------------------------------------

@router.post(
    "/check-duplicates",
    response_model=DuplicateCheckResponse,
    summary="Check which job URLs are already known",
    description=(
        "Accepts a list of job URLs and returns two buckets: "
        "URLs already in the processed store (`duplicates`) and "
        "URLs that are genuinely new (`new`)."
    ),
)
async def check_duplicates(body: DuplicateCheckRequest) -> DuplicateCheckResponse:
    duplicates: list[str] = []
    new: list[str] = []
    for url in body.urls:
        if url and url in _processed_urls:
            duplicates.append(url)
        else:
            new.append(url)
    return DuplicateCheckResponse(
        total_checked=len(body.urls),
        duplicates=duplicates,
        new=new,
    )


# ---------------------------------------------------------------------------
# PATCH /api/v1/jobs/mark-processed
# ---------------------------------------------------------------------------

@router.patch(
    "/mark-processed",
    response_model=MarkProcessedResponse,
    summary="Mark job URLs as processed",
    description=(
        "Adds the supplied job URLs to the in-memory processed store. "
        "Subsequent calls to `check-duplicates` or `unprocessed` will "
        "exclude these URLs from results."
    ),
)
async def mark_processed(body: MarkProcessedRequest) -> MarkProcessedResponse:
    before = len(_processed_urls)
    valid_urls = [u for u in body.urls if u]
    _processed_urls.update(valid_urls)
    newly_marked = len(_processed_urls) - before
    return MarkProcessedResponse(
        marked=newly_marked,
        total_processed=len(_processed_urls),
        message=f"{newly_marked} new URL(s) added; {len(_processed_urls)} total in store.",
    )


# ---------------------------------------------------------------------------
# GET /api/v1/jobs/unprocessed?category=<slug>
# ---------------------------------------------------------------------------

@router.get(
    "/unprocessed",
    response_model=UnprocessedJobsResponse,
    summary="Scrape unprocessed jobs for a category",
    description=(
        "Scrapes all roles in the given category and returns only fresh jobs whose "
        "URL has **not** been marked as processed yet, enriched with decision-maker contacts.\n\n"
        "- Use `GET /api/v1/categories` to discover valid `category` slugs.\n"
        "- **hours_old**: Max job age in hours (default: 48, for 1–2 days old fresh jobs)\n"
        "- **enrich_contacts**: Enrich company with HR/Executive contacts, LinkedIn and email (default: True)\n"
        "- After consuming the results, call `PATCH /api/v1/jobs/mark-processed` "
        "to exclude them from future responses."
    ),
)
async def get_unprocessed_jobs(
    category: str = Query(
        ...,
        description="Category slug (e.g. 'IT', 'healthcare'). See GET /api/v1/categories.",
        examples=["IT", "healthcare", "finance"],
    ),
) -> UnprocessedJobsResponse:
    hours_old = 48
    # --- Resolve slug → industry name ---
    industry_name = CATEGORY_SLUG_MAP.get(category)
    if not industry_name:
        valid = ", ".join(CATEGORY_SLUG_MAP.keys())
        raise HTTPException(
            status_code=404,
            detail=f"Unknown category slug '{category}'. Valid slugs: {valid}",
        )

    roles = ALL_ROLES_BY_INDUSTRY.get(industry_name, [])
    if not roles:
        raise HTTPException(
            status_code=404,
            detail=f"No roles found for category '{industry_name}'.",
        )

    logger.info(
        "unprocessed[%s]: scraping %d roles in '%s'",
        category, len(roles), industry_name,
    )

    # --- Scrape all roles concurrently (cap concurrency with semaphore) ---
    sem = asyncio.Semaphore(10)

    async def _scrape_with_sem(role: str) -> RoleResult:
        async with sem:
            return await _scrape_role(role, industry_name, hours_old=hours_old)

    role_results: list[RoleResult] = await asyncio.gather(
        *[_scrape_with_sem(role) for role in roles]
    )

    # --- Flatten all listings ---
    all_jobs: list[JobListing] = []
    for result in role_results:
        all_jobs.extend(result.jobs)

    total_scraped = len(all_jobs)

    # --- Filter out already-processed URLs ---
    unprocessed_jobs = [
        job for job in all_jobs
        if not job.url or job.url not in _processed_urls
    ]

    # --- Enrich unprocessed jobs with company decision-makers ---
    if unprocessed_jobs:
        logger.info("Enriching %d unprocessed listings with decision-makers...", len(unprocessed_jobs))
        await enricher.enrich_listings(unprocessed_jobs)

    logger.info(
        "unprocessed[%s]: %d scraped, %d unprocessed (%d filtered)",
        category,
        total_scraped,
        len(unprocessed_jobs),
        total_scraped - len(unprocessed_jobs),
    )

    return UnprocessedJobsResponse(
        category_slug=category,
        category_name=industry_name,
        location=SCRAPE_LOCATION,
        total_scraped=total_scraped,
        total_unprocessed=len(unprocessed_jobs),
        jobs=unprocessed_jobs,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/trigger/{category}
# ---------------------------------------------------------------------------

@trigger_router.get(
    "/trigger/{category}",
    summary="Trigger the n8n recruitment scrape webhook",
    description=(
        "Forwards a GET request to the n8n webhook for the given category, "
        "used to kick off the external n8n scraping workflow."
    ),
)
async def trigger_workflow(category: str):
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                N8N_WEBHOOK,
                params={"category": category},
                timeout=30.0,
            )
    except httpx.RequestError as exc:
        logger.exception("Failed to reach n8n webhook for category '%s'", category)
        raise HTTPException(
            status_code=502,
            detail=f"Could not reach n8n webhook: {exc}",
        )

    return {
        "status": "triggered",
        "category": category,
        "n8n_status": response.status_code,
    }