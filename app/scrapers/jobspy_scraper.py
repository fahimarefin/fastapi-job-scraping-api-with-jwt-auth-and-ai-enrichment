#app/scrapers/jobspy_scraper.py
from __future__ import annotations

import asyncio
import logging
import math
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pandas as pd

from app.models.schemas import JobListing, JobSource
from app.scrapers.base import BaseScraper

logger = logging.getLogger(__name__)

# One reusable executor — keeps the thread count bounded
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="jobspy")

# Map jobspy site name strings → our JobSource enum values
_SITE_TO_SOURCE: dict[str, JobSource] = {
    "indeed": JobSource.INDEED,
    "linkedin": JobSource.LINKEDIN,
    "glassdoor": JobSource.GLASSDOOR,
    "zip_recruiter": JobSource.ZIP_RECRUITER,
}

# These sites 403 from a plain server IP without a rotating proxy list.
_PROXY_REQUIRED_SITES: frozenset[str] = frozenset({"glassdoor", "zip_recruiter"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_str(value: Any) -> str | None:
    """Return None for NaN / None / empty, otherwise a clean str."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    s = str(value).strip()
    return s if s and s.lower() != "nan" else None


def _safe_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "yes", "1", "remote")
    return None


def _df_to_listings(df: pd.DataFrame, site: str) -> list[JobListing]:
    """Convert a jobspy DataFrame into a list of JobListing models."""
    source = _SITE_TO_SOURCE.get(site.lower(), JobSource.INDEED)
    listings: list[JobListing] = []

    for _, row in df.iterrows():
        title = _safe_str(row.get("title"))
        if not title:
            continue

        # Build a human-readable salary string from the numeric columns
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

        listings.append(
            JobListing(
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
        )
    return listings


def _scrape_one_site_sync(
    site: str,
    query: str,
    location: str | None,
    limit: int,
    country_indeed: str,
    proxies: list[str] | None,
) -> tuple[str, pd.DataFrame | None, str | None]:
    """
    Scrape a single site synchronously (runs in a thread pool).
    Returns (site, dataframe_or_None, error_message_or_None).
    """
    from jobspy import scrape_jobs  # lazy import — keeps startup fast

    try:
        df = scrape_jobs(
            site_name=[site],
            search_term=query,
            location=location or "",
            results_wanted=limit,
            country_indeed=country_indeed,
            proxies=proxies,
            verbose=False,
        )
        return site, df, None
    except Exception as exc:  # noqa: BLE001
        return site, None, str(exc)


# ---------------------------------------------------------------------------
# Scraper class
# ---------------------------------------------------------------------------

class JobSpyScraper(BaseScraper):
    """
    Wraps the `jobspy` library to scrape one or more job boards.

    Each site is scraped in its own thread so a 403 on one board
    never eats the result quota of the others.

    Proxy-required sites (glassdoor, zip_recruiter) are automatically
    skipped when no proxies are configured.
    """

    source_name = "jobspy"

    def __init__(
        self,
        sites: list[str] | None = None,
        country_indeed: str = "USA",
        proxies: list[str] | None = None,
    ) -> None:
        self.all_sites = sites or ["indeed", "linkedin", "glassdoor", "zip_recruiter"]
        self.country_indeed = country_indeed
        self.proxies = proxies

    @property
    def active_sites(self) -> list[str]:
        """Sites we will actually attempt (skip proxy-required ones when no proxies)."""
        if self.proxies:
            return self.all_sites
        return [s for s in self.all_sites if s not in _PROXY_REQUIRED_SITES]

    async def search(self, query: str, location: str | None, limit: int) -> list[JobListing]:
        loop = asyncio.get_running_loop()
        sites = self.active_sites

        if not sites:
            logger.warning("No active sites to scrape (all require proxies).")
            return []

        # Scrape each site in parallel threads — one thread per site so each
        # gets its own full `limit` budget regardless of other sites' failures.
        futures = [
            loop.run_in_executor(
                _executor,
                _scrape_one_site_sync,
                site,
                query,
                location,
                limit,
                self.country_indeed,
                self.proxies,
            )
            for site in sites
        ]
        results = await asyncio.gather(*futures, return_exceptions=False)

        all_listings: list[JobListing] = []
        for site, df, error in results:  # type: ignore[misc]
            if error:
                logger.error("JobSpy[%s] failed: %s", site, error)
                continue
            if df is None or df.empty:
                logger.info("JobSpy[%s] returned 0 results.", site)
                continue
            listings = _df_to_listings(df, site)
            logger.info("JobSpy[%s] returned %d listings.", site, len(listings))
            all_listings.extend(listings[:limit])

        return all_listings
