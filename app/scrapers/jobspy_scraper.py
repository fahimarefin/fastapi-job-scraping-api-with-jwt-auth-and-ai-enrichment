"""
JobSpy-backed scraper.

jobspy (python-jobspy) wraps Indeed, LinkedIn, Glassdoor, ZipRecruiter and
Google Jobs under one `scrape_jobs()` call.  Because it is fully synchronous
(uses `requests` internally) we run it in a thread-pool executor so we never
block the FastAPI event loop.
"""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pandas as pd

from app.models.schemas import JobListing, JobSource
from app.scrapers.base import BaseScraper

logger = logging.getLogger(__name__)

# One reusable executor — keeps the thread count bounded
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="jobspy")

# Map jobspy site names → our JobSource enum values
_SITE_TO_SOURCE: dict[str, JobSource] = {
    "indeed": JobSource.INDEED,
    "linkedin": JobSource.LINKEDIN,
    "glassdoor": JobSource.GLASSDOOR,
    "zip_recruiter": JobSource.ZIP_RECRUITER,
}


def _safe_str(value: Any) -> str | None:
    """Return None for NaN / None / empty, otherwise str(value)."""
    if value is None:
        return None
    try:
        import math
        if isinstance(value, float) and math.isnan(value):
            return None
    except Exception:
        pass
    s = str(value).strip()
    return s if s and s.lower() != "nan" else None


def _safe_bool(value: Any) -> bool | None:
    if value is None:
        return None
    try:
        import math
        if isinstance(value, float) and math.isnan(value):
            return None
    except Exception:
        pass
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "yes", "1", "remote")
    return None


def _row_to_listing(row: pd.Series, source: JobSource) -> JobListing | None:
    title = _safe_str(row.get("title"))
    if not title:
        return None

    # Salary: prefer the formatted interval string if present
    min_sal = _safe_str(row.get("min_amount"))
    max_sal = _safe_str(row.get("max_amount"))
    currency = _safe_str(row.get("currency")) or ""
    interval = _safe_str(row.get("interval")) or ""
    if min_sal and max_sal:
        salary = f"{currency}{min_sal}–{currency}{max_sal} {interval}".strip()
    elif min_sal or max_sal:
        salary = f"{currency}{min_sal or max_sal} {interval}".strip()
    else:
        salary = None

    return JobListing(
        title=title,
        company=_safe_str(row.get("company")),
        location=_safe_str(row.get("location")),
        description=_safe_str(row.get("description")),
        url=_safe_str(row.get("job_url")),
        posted_at=_safe_str(row.get("date_posted")),
        salary=salary,
        job_type=_safe_str(row.get("job_type")),
        is_remote=_safe_bool(row.get("is_remote")),
        source=source,
    )


class JobSpyScraper(BaseScraper):
    """
    Wraps the `jobspy` library to scrape one or more job boards at once.

    Usage:
        scraper = JobSpyScraper(sites=["indeed", "linkedin"])
        jobs = await scraper.search("Python Developer", "Remote", 25)
    """

    source_name = "jobspy"

    def __init__(
        self,
        sites: list[str] | None = None,
        country_indeed: str = "USA",
        proxies: list[str] | None = None,
    ) -> None:
        # Default: scrape Indeed + LinkedIn + Glassdoor + ZipRecruiter
        self.sites = sites or ["indeed", "linkedin", "glassdoor", "zip_recruiter"]
        self.country_indeed = country_indeed
        self.proxies = proxies

    def _scrape_sync(self, query: str, location: str | None, limit: int) -> pd.DataFrame:
        """Called inside a thread — may block."""
        from jobspy import scrape_jobs  # imported here to keep startup fast

        return scrape_jobs(
            site_name=self.sites,
            search_term=query,
            location=location or "",
            results_wanted=limit,
            country_indeed=self.country_indeed,
            proxies=self.proxies,
            verbose=False,
        )

    async def search(self, query: str, location: str | None, limit: int) -> list[JobListing]:
        loop = asyncio.get_running_loop()
        try:
            df: pd.DataFrame = await loop.run_in_executor(
                _executor, self._scrape_sync, query, location, limit
            )
        except Exception as exc:
            logger.error("JobSpy scrape failed: %s", exc)
            raise

        if df is None or df.empty:
            return []

        listings: list[JobListing] = []
        for _, row in df.iterrows():
            site_raw = _safe_str(row.get("site")) or "indeed"
            source = _SITE_TO_SOURCE.get(site_raw.lower(), JobSource.INDEED)
            listing = _row_to_listing(row, source)
            if listing:
                listings.append(listing)

        return listings[:limit]
