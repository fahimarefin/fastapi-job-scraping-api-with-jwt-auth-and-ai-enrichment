#app/api/routes.py
"""
Job scraping routes.

All configuration is hardcoded — no query parameters are accepted.
  • Location  : USA (always)
  • Limit      : 50 results per role per source
  • Roles      : every role in ALL_ROLES_BY_INDUSTRY (defined in schemas.py)
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
# Internal helper — scrape a single role across all configured sources
# ---------------------------------------------------------------------------

async def _scrape_role(role: str, industry: str) -> RoleResult:
    """Run all scrapers for one role and return a RoleResult."""
    errors: dict[str, str] = {}
    jobs: list[JobListing] = []

    # JobSpy (Indeed + LinkedIn + Glassdoor + ZipRecruiter)
    try:
        listings = await _jobspy.search(role, SCRAPE_LOCATION, SCRAPE_LIMIT)
        jobs.extend(listings[:SCRAPE_LIMIT])
    except Exception as exc:  # noqa: BLE001
        logger.exception("JobSpy failed for role '%s'", role)
        for site in _jobspy.active_sites:
            errors[site] = str(exc)

    # Career pages
    try:
        listings = await _career_page_scraper.search(role, SCRAPE_LOCATION, SCRAPE_LIMIT)
        jobs.extend(listings[:SCRAPE_LIMIT])
    except Exception as exc:  # noqa: BLE001
        logger.exception("CareerPage scraper failed for role '%s'", role)
        errors[JobSource.CAREER_PAGE.value] = str(exc)

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
# No parameters — scrapes every hardcoded role in every industry
# ---------------------------------------------------------------------------

@router.get(
    "/scrape-all",
    response_model=ScrapeAllResponse,
    summary="Scrape all roles (no parameters needed)",
    description=(
        "Automatically scrapes **50 jobs** per role for **every hardcoded role** "
        "across all 10 industries.\n\n"
        "- **Location**: USA (fixed)\n"
        "- **Limit**: 50results per role per source (fixed)\n"
        "- **Roles**: fully hardcoded — no input required\n\n"
        "> ⚠️ This endpoint may take several minutes due to the volume of requests."
    ),
)
async def scrape_all() -> ScrapeAllResponse:
    """
    Triggers scraping for every predefined role across all job boards.
    No parameters needed — location is USA, limit is 50, roles are hardcoded.
    """
    industries_result: dict[str, list[RoleResult]] = defaultdict(list)
    total_jobs = 0

    # Build a flat task list: (role, industry) pairs — run roles in an industry concurrently,
    # but process industries sequentially to avoid flooding the job boards.
    for industry, roles in ALL_ROLES_BY_INDUSTRY.items():
        logger.info("Scraping industry: %s (%d roles)", industry, len(roles))

        # Run all roles within this industry concurrently
        role_tasks = [_scrape_role(role, industry) for role in roles]
        role_results: list[RoleResult] = await asyncio.gather(*role_tasks)

        for result in role_results:
            industries_result[industry].append(result)
            total_jobs += result.total
            logger.info(
                "  [%s] '%s' → %d jobs", industry, result.role, result.total
            )

    total_roles = sum(len(v) for v in industries_result.values())

    return ScrapeAllResponse(
        location=SCRAPE_LOCATION,
        results_per_role_requested=SCRAPE_LIMIT,
        total_roles_scraped=total_roles,
        total_jobs_found=total_jobs,
        industries=dict(industries_result),
    )