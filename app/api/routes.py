#app/api/routes.py
import asyncio
import logging
from collections import defaultdict
from fastapi import APIRouter, Query
from app.models.schemas import AggregatedSearchResponse, JobListing, JobSource
from app.scrapers.career_pages import CareerPageScraper
from app.scrapers.jobspy_scraper import JobSpyScraper

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])

# JobSpy handles Indeed, LinkedIn, Glassdoor, ZipRecruiter in one call.
# CareerPageScraper handles custom company career pages independently.
_jobspy = JobSpyScraper(sites=["indeed", "linkedin", "glassdoor", "zip_recruiter"])
_career_page_scraper = CareerPageScraper()


@router.get("/search", response_model=AggregatedSearchResponse)
async def search_all(
    query: str = Query(..., description="Job title, keywords, etc."),
    location: str | None = Query(None, description="City, state, country, or 'Remote'"),
    limit: int = Query(25, ge=1, le=100, description="Max results per source"),
):
    errors: dict[str, str] = {}
    results_by_source: dict[str, list[JobListing]] = defaultdict(list)

    # --- JobSpy: scrapes multiple boards in one blocking call (run in thread) ---
    async def run_jobspy() -> None:
        try:
            listings = await _jobspy.search(query, location, limit)
            for job in listings:
                results_by_source[job.source.value].append(job)
        except Exception as exc:
            logger.exception("jobspy scrape failed")
            for site in _jobspy.active_sites:
                errors[site] = str(exc)

    # --- Career pages: custom HTML scraping ---
    async def run_career_pages() -> None:
        try:
            listings = await _career_page_scraper.search(query, location, limit)
            results_by_source[JobSource.CAREER_PAGE.value].extend(listings)
        except Exception as exc:
            logger.exception("career_page scrape failed")
            errors[JobSource.CAREER_PAGE.value] = str(exc)

    await asyncio.gather(run_jobspy(), run_career_pages())

    # Enforce per-source limit (jobspy may return more than requested)
    for source_key in results_by_source:
        results_by_source[source_key] = results_by_source[source_key][:limit]

    total = sum(len(v) for v in results_by_source.values())

    return AggregatedSearchResponse(
        query=query,
        location=location,
        total_results=total,
        results_by_source=dict(results_by_source),
        errors=errors,
    )