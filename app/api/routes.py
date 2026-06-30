import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query

from app.models.schemas import (
    AggregatedSearchResponse,
    JobSearchResponse,
    JobSource,
)
from app.scrapers.career_pages import CareerPageScraper
from app.scrapers.indeed import IndeedScraper
from app.scrapers.linkedin import LinkedInScraper

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])

_scrapers = {
    JobSource.INDEED: IndeedScraper(),
    JobSource.LINKEDIN: LinkedInScraper(),
    JobSource.CAREER_PAGE: CareerPageScraper(),
}


# @router.get("/indeed", response_model=JobSearchResponse)
# async def search_indeed(
#     query: str = Query(..., description="Job title or keywords"),
#     location: str | None = Query(None),
#     limit: int = Query(25, ge=1, le=100),
# ):
#     try:
#         results = await _scrapers[JobSource.INDEED].search(query, location, limit)
#     except Exception as exc:  # noqa: BLE001
#         logger.exception("Indeed scrape failed")
#         raise HTTPException(status_code=502, detail=f"Indeed scrape failed: {exc}") from exc

#     return JobSearchResponse(
#         source=JobSource.INDEED, query=query, location=location, count=len(results), results=results
#     )


# @router.get("/linkedin", response_model=JobSearchResponse)
# async def search_linkedin(
#     query: str = Query(..., description="Job title or keywords"),
#     location: str | None = Query(None),
#     limit: int = Query(25, ge=1, le=100),
# ):
#     try:
#         results = await _scrapers[JobSource.LINKEDIN].search(query, location, limit)
#     except Exception as exc:  # noqa: BLE001
#         logger.exception("LinkedIn scrape failed")
#         raise HTTPException(status_code=502, detail=f"LinkedIn scrape failed: {exc}") from exc

#     return JobSearchResponse(
#         source=JobSource.LINKEDIN, query=query, location=location, count=len(results), results=results
#     )


# @router.get("/career-pages", response_model=JobSearchResponse)
# async def search_career_pages(
#     query: str = Query("", description="Job title keyword filter, empty returns all"),
#     location: str | None = Query(None),
#     limit: int = Query(25, ge=1, le=100),
# ):
#     try:
#         results = await _scrapers[JobSource.CAREER_PAGE].search(query, location, limit)
#     except Exception as exc:  # noqa: BLE001
#         logger.exception("Career page scrape failed")
#         raise HTTPException(status_code=502, detail=f"Career page scrape failed: {exc}") from exc

#     return JobSearchResponse(
#         source=JobSource.CAREER_PAGE, query=query, location=location, count=len(results), results=results
#     )


@router.get("/search", response_model=AggregatedSearchResponse)
async def search_all(
    query: str = Query(..., description="Job title or keywords"),
    location: str | None = Query(None),
    limit: int = Query(25, ge=1, le=100),
):
    """Search Indeed, LinkedIn, and configured career pages concurrently."""

    async def run(source: JobSource):
        try:
            return source, await _scrapers[source].search(query, location, limit), None
        except Exception as exc:  # noqa: BLE001
            logger.exception("%s scrape failed", source.value)
            return source, [], str(exc)

    outcomes = await asyncio.gather(*(run(s) for s in _scrapers))

    results_by_source: dict[str, list] = {}
    errors: dict[str, str] = {}
    total = 0

    for source, results, error in outcomes:
        results_by_source[source.value] = results
        total += len(results)
        if error:
            errors[source.value] = error

    return AggregatedSearchResponse(
        query=query,
        location=location,
        total_results=total,
        results_by_source=results_by_source,
        errors=errors,
    )
