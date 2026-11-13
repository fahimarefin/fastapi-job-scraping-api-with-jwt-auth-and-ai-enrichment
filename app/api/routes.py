#app/api/routes.py
import logging
from functools import lru_cache

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pymongo.errors import PyMongoError

from app.models.schemas import (
    ALL_ROLES_BY_INDUSTRY,
    CATEGORY_SLUG_MAP,
    SCRAPE_LOCATION,
    CategoriesResponse,
    CategoryInfo,
    DuplicateCheckRequest,
    DuplicateCheckResponse,
    MarkProcessedRequest,
    MarkProcessedResponse,
    ScrapeAllResponse,
    UnprocessedJobsResponse,
)
from app.services.job_store import JobStore

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])
categories_router = APIRouter(prefix="/api/v1", tags=["categories"])
trigger_router = APIRouter(prefix="/api/v1", tags=["trigger"])

N8N_WEBHOOK = "https://n8n.srv1795032.hstgr.cloud/webhook-test/recruitment/scrape"

@lru_cache
def get_job_store() -> JobStore:
    return JobStore(cached_reads=True)


async def _database_call(operation):
    try:
        return await operation
    except PyMongoError as exc:
        logger.error("Job database unavailable (%s)", type(exc).__name__)
        raise HTTPException(503, "Job database is temporarily unavailable") from exc


@router.get(
    "/scrape-all",
    response_model=ScrapeAllResponse,
    summary="Get scheduled job results (no parameters needed)",
    description=(
        "Returns prepared MongoDB job data through a background-loaded API cache. The backend scheduler refreshes sources independently. "
        "USA jobs posted within the last 48 hours, up to 800 unique jobs across all sources and industries. "
        "The existing industry/role response includes last_updated_at, refresh_status and source_statuses. "
        "During refresh, the previous saved result is served. Before the first result is saved, "
        "returns an empty job list with ready=false."
    ),
)
async def scrape_all(store: JobStore = Depends(get_job_store)) -> ScrapeAllResponse:
    return await _database_call(store.response())


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
async def check_duplicates(body: DuplicateCheckRequest, store: JobStore = Depends(get_job_store)) -> DuplicateCheckResponse:
    known = await _database_call(store.known_urls(body.urls))
    return DuplicateCheckResponse(
        total_checked=len(body.urls),
        duplicates=[url for url in body.urls if url and url in known],
        new=[url for url in body.urls if not url or url not in known],
    )


# ---------------------------------------------------------------------------
# PATCH /api/v1/jobs/mark-processed
# ---------------------------------------------------------------------------

@router.patch(
    "/mark-processed",
    response_model=MarkProcessedResponse,
    summary="Mark job URLs as processed",
    description=(
        "Adds the supplied job URLs to the persistent processed store. "
        "Subsequent calls to `check-duplicates` or `unprocessed` will "
        "exclude these URLs from results."
    ),
)
async def mark_processed(body: MarkProcessedRequest, store: JobStore = Depends(get_job_store)) -> MarkProcessedResponse:
    marked, total = await _database_call(store.mark_processed(body.urls))
    return MarkProcessedResponse(
        marked=marked, total_processed=total,
        message=f"{marked} new URL(s) added; {total} total in store.",
    )


# ---------------------------------------------------------------------------
# GET /api/v1/jobs/unprocessed?category=<slug>
# ---------------------------------------------------------------------------

@router.get(
    "/unprocessed",
    response_model=UnprocessedJobsResponse,
    summary="Get saved unprocessed jobs for a category",
    description=(
        "Reads the scheduled job result for a category from MongoDB, excluding processed URLs. "
        "Use GET /api/v1/categories for category slugs. "
        "Call PATCH /api/v1/jobs/mark-processed after consuming jobs."
    ),
)
async def get_unprocessed_jobs(
    category: str = Query(..., description="Category slug from GET /api/v1/categories", examples=["IT"]),
    store: JobStore = Depends(get_job_store),
) -> UnprocessedJobsResponse:
    industry_name = CATEGORY_SLUG_MAP.get(category)
    if not industry_name:
        raise HTTPException(404, f"Unknown category slug '{category}'. Valid slugs: {', '.join(CATEGORY_SLUG_MAP)}")
    snapshot = await _database_call(store.response())
    jobs = [job for role in snapshot.industries.get(industry_name, []) for job in role.jobs]
    known = await _database_call(store.known_urls([job.url for job in jobs if job.url]))
    unprocessed = [job for job in jobs if not job.url or job.url not in known]
    return UnprocessedJobsResponse(
        category_slug=category, category_name=industry_name, location=SCRAPE_LOCATION,
        total_scraped=len(jobs), total_unprocessed=len(unprocessed), jobs=unprocessed,
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