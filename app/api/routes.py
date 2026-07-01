#app/api/routes.py
"""
Job scraping routes.

All configuration is hardcoded — no query parameters are accepted.
  • Location  : USA (always)
  • Limit      : 20 results per role per source
  • Roles      : every role in ALL_ROLES_BY_INDUSTRY (defined in schemas.py)

Performance design:
  1. Roles are DEDUPLICATED across industries before scraping.
     "Office Manager" appears in 2 industries but is scraped only once;
     its results are copied to both industries in the response.
  2. JobSpy and CareerPageScraper run CONCURRENTLY inside each role task.
  3. All unique roles are gathered at once, capped by a semaphore.
"""
import asyncio
import logging
from collections import defaultdict

from fastapi import APIRouter

from app.models.schemas import (
    ALL_ROLES_BY_INDUSTRY,
    SCRAPE_LIMIT,
    SCRAPE_LOCATION,
    JobListing,
    JobSource,
    RoleResult,
    ScrapeAllResponse,
)
from app.scrapers.career_pages import CareerPageScraper
from app.scrapers.jobspy_scraper import JobSpyScraper

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])

_jobspy = JobSpyScraper(sites=["indeed", "linkedin", "glassdoor", "zip_recruiter"])
_career_page_scraper = CareerPageScraper()


# ---------------------------------------------------------------------------
# Internal helper — scrape a single role across all sources CONCURRENTLY
# ---------------------------------------------------------------------------

async def _scrape_role(role: str, industry: str) -> RoleResult:
    """
    Run JobSpy and CareerPageScraper for one role at the same time.
    Previously they ran sequentially; now career-page fetching overlaps
    with the (slower) JobSpy thread-pool work.
    """
    errors: dict[str, str] = {}

    # Launch both scrapers simultaneously
    jobspy_task = asyncio.ensure_future(_jobspy.search(role, SCRAPE_LOCATION, SCRAPE_LIMIT))
    career_task = asyncio.ensure_future(_career_page_scraper.search(role, SCRAPE_LOCATION, SCRAPE_LIMIT))

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
        jobspy_listings = jobspy_result[:SCRAPE_LIMIT]  # type: ignore[index]

    if isinstance(career_result, Exception):
        logger.exception("CareerPage scraper failed for role '%s': %s", role, career_result)
        errors[JobSource.CAREER_PAGE.value] = str(career_result)
    else:
        career_listings = career_result[:SCRAPE_LIMIT]  # type: ignore[index]

    jobs = jobspy_listings + career_listings

    return RoleResult(
        role=role,
        industry=industry,
        location=SCRAPE_LOCATION,
        total=len(jobs),
        jobs=jobs,
        errors=errors,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/jobs/scrape-all
# ---------------------------------------------------------------------------

@router.get(
    "/scrape-all",
    response_model=ScrapeAllResponse,
    summary="Scrape all roles (no parameters needed)",
    description=(
        "Automatically scrapes **20 jobs** per role for **every hardcoded role** "
        "across all 10 industries.\n\n"
        "- **Location**: USA (fixed)\n"
        "- **Limit**: 20 results per role per source (fixed)\n"
        "- **Roles**: fully hardcoded — no input required\n\n"
        "> ⚠️ This endpoint may take several minutes due to the volume of requests."
    ),
)
async def scrape_all() -> ScrapeAllResponse:
    """
    Scrapes every predefined role across all job boards.

    Optimisations:
    - Roles are deduplicated: a role shared by N industries is scraped once
      and the result is copied to all N industries.
    - JobSpy + career pages run concurrently per role.
    - A semaphore limits peak concurrent role tasks to avoid flooding boards.
    - Career page HTML is cached (5 min TTL) so the same URL is only fetched
      once per scrape-all run regardless of how many roles query it.
    """
    # Build unique-role → [industries] mapping
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
    MAX_CONCURRENT_ROLES = 30
    sem = asyncio.Semaphore(MAX_CONCURRENT_ROLES)

    async def _scrape_unique_role(role: str) -> RoleResult:
        async with sem:
            # Use the first industry as the canonical one for the scrape call
            primary_industry = role_to_industries[role][0]
            return await _scrape_role(role, primary_industry)

    # Fire all unique roles concurrently
    all_results: list[RoleResult] = await asyncio.gather(
        *[_scrape_unique_role(role) for role in unique_roles]
    )

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
                    jobs=result.jobs,
                    errors=result.errors,
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