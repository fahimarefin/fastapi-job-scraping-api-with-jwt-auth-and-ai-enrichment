# app/api/candidate_routes.py
"""
API routes for self-hosted LinkedIn candidate scraping.

Endpoints:
  POST /api/v1/candidates/search   — search by keyword / title / location
  POST /api/v1/candidates/profile  — fetch a single profile by LinkedIn URL
"""
import logging

from fastapi import APIRouter, HTTPException

from app.models.schemas import (
    CandidateProfileRequest,
    CandidateProfileResponse,
    CandidateSearchRequest,
    CandidateSearchResponse,
)
from app.scrapers.candidate_scraper import LinkedInCandidateScraper

logger = logging.getLogger(__name__)
candidates_router = APIRouter(prefix="/api/v1/candidates", tags=["candidates"])

_scraper = LinkedInCandidateScraper()


# ---------------------------------------------------------------------------
# POST /api/v1/candidates/search
# ---------------------------------------------------------------------------

@candidates_router.post(
    "/search",
    response_model=CandidateSearchResponse,
    summary="Search for candidates by keyword",
    description=(
        "Searches LinkedIn for candidate profiles matching the given keyword "
        "using DuckDuckGo discovery + public profile scraping.\n\n"
        "- **keyword** (required): search term, e.g. 'Python Developer'\n"
        "- **job_title**: optional filter by current job title\n"
        "- **location**: optional filter by location\n"
        "- **limit**: max candidates to return (1–50, default 10)\n\n"
        "> ⏱️ This endpoint may take 20–60 seconds depending on the number "
        "of profiles requested, due to rate-limiting between requests."
    ),
)
async def search_candidates(body: CandidateSearchRequest) -> CandidateSearchResponse:
    try:
        candidates = await _scraper.search_candidates(
            keyword=body.keyword,
            job_title=body.job_title,
            location=body.location,
            limit=body.limit,
        )
    except RuntimeError as exc:
        logger.error("Candidate search failed: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        logger.exception("Unexpected error during candidate search")
        raise HTTPException(status_code=500, detail=f"Internal error: {exc}")

    return CandidateSearchResponse(
        keyword=body.keyword,
        total=len(candidates),
        candidates=candidates,
    )


# ---------------------------------------------------------------------------
# POST /api/v1/candidates/profile
# ---------------------------------------------------------------------------

@candidates_router.post(
    "/profile",
    response_model=CandidateProfileResponse,
    summary="Fetch a single candidate profile by LinkedIn URL",
    description=(
        "Given a full LinkedIn profile URL (e.g. "
        "`https://www.linkedin.com/in/username`), fetches the public profile "
        "page and returns structured data (name, headline, location, etc.).\n\n"
        "> ⏱️ Typically responds in 3–10 seconds."
    ),
)
async def get_candidate_profile(body: CandidateProfileRequest) -> CandidateProfileResponse:
    # Basic URL validation
    if "linkedin.com/in/" not in body.linkedin_url:
        raise HTTPException(
            status_code=422,
            detail=(
                "Invalid LinkedIn URL. Expected format: "
                "https://www.linkedin.com/in/username"
            ),
        )

    try:
        profile = await _scraper.lookup_profile(body.linkedin_url)
    except Exception as exc:
        logger.exception("Unexpected error during profile lookup")
        return CandidateProfileResponse(
            linkedin_url=body.linkedin_url,
            profile=None,
            error=f"Failed to fetch profile: {exc}",
        )

    if profile is None:
        return CandidateProfileResponse(
            linkedin_url=body.linkedin_url,
            profile=None,
            error="Profile not found or blocked by LinkedIn. Try again later.",
        )

    return CandidateProfileResponse(
        linkedin_url=body.linkedin_url,
        profile=profile,
    )
