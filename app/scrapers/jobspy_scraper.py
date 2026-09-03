#app/scrapers/jobspy_scraper.py
from __future__ import annotations

import asyncio
import logging
import math
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from app.models.schemas import JobListing, JobSource
from app.scrapers.base import BaseScraper

logger = logging.getLogger(__name__)

# One reusable executor — keeps the thread count bounded
_executor = ThreadPoolExecutor(max_workers=32, thread_name_prefix="jobspy")

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


def _parse_post_date(value: Any) -> tuple[str | None, str | None, float | None, bool | None]:
    """
    Parse date_posted from jobspy or raw date string.
    Returns: (raw_str, date_str_iso, days_old, is_fresh)
    """
    if value is None:
        return None, None, None, None
    raw_str = str(value).strip()
    if not raw_str or raw_str.lower() in ("nan", "none", "nat"):
        return None, None, None, None

    now = datetime.now()
    today_date = now.date()

    # 1. Check if datetime.date or datetime.datetime
    if isinstance(value, (datetime, pd.Timestamp)):
        dt = value.to_pydatetime() if hasattr(value, "to_pydatetime") else value
        diff_days = (now - dt).total_seconds() / 86400.0
        diff_days = max(0.0, round(diff_days, 1))
        return raw_str, dt.strftime("%Y-%m-%d"), diff_days, (diff_days <= 2.5)
    elif isinstance(value, date):
        diff_days = max(0.0, float((today_date - value).days))
        return raw_str, value.isoformat(), diff_days, (diff_days <= 2.5)

    # 2. Check ISO format YYYY-MM-DD
    match_iso = re.search(r'(\d{4})-(\d{2})-(\d{2})', raw_str)
    if match_iso:
        try:
            parsed_d = datetime.strptime(match_iso.group(0), "%Y-%m-%d").date()
            diff_days = max(0.0, float((today_date - parsed_d).days))
            return raw_str, parsed_d.isoformat(), diff_days, (diff_days <= 2.5)
        except ValueError:
            pass

    # 3. Relative descriptions like "1 day ago", "2 days ago", "12 hours ago", "Just posted", "Today"
    lower = raw_str.lower()
    if any(term in lower for term in ("hour", "hr", "minute", "min", "just", "today", "now")):
        return raw_str, today_date.isoformat(), 0.1, True

    day_match = re.search(r'(\d+)\s*day', lower)
    if day_match:
        d = float(day_match.group(1))
        post_d = (today_date - timedelta(days=int(d))).isoformat()
        return raw_str, post_d, d, (d <= 2.5)

    if "yesterday" in lower:
        post_d = (today_date - timedelta(days=1)).isoformat()
        return raw_str, post_d, 1.0, True

    if any(term in lower for term in ("week", "month", "year", "30+")):
        return raw_str, None, 7.0, False

    return raw_str, None, None, None


def _df_to_listings(
    df: pd.DataFrame,
    site: str,
    max_hours_old: int | None = None,
) -> list[JobListing]:
    """Convert a jobspy DataFrame into a list of JobListing models."""
    source = _SITE_TO_SOURCE.get(site.lower(), JobSource.INDEED)
    listings: list[JobListing] = []

    # Maximum age in days threshold (e.g. 48h = 2 days + grace period)
    max_days_threshold = ((max_hours_old / 24.0) + 0.5) if max_hours_old else None

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

        raw_posted, posted_date, days_old, is_fresh = _parse_post_date(row.get("date_posted"))

        # If hours_old is set and this listing is demonstrably older, filter it out
        if max_days_threshold is not None and days_old is not None:
            if days_old > max_days_threshold:
                continue

        listings.append(
            JobListing(
                title=title,
                company=_safe_str(row.get("company")),
                location=_safe_str(row.get("location")),
                description=_safe_str(row.get("description")),
                url=_safe_str(row.get("job_url")),
                posted_at=raw_posted,
                posted_date=posted_date,
                days_old=days_old,
                is_fresh=is_fresh if is_fresh is not None else (days_old is not None and days_old <= 2.5),
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
    hours_old: int | None = 48,
) -> tuple[str, pd.DataFrame | None, str | None]:
    """
    Scrape a single site synchronously (runs in a thread pool).
    Returns (site, dataframe_or_None, error_message_or_None).
    """
    from jobspy import scrape_jobs  # lazy import — keeps startup fast

    try:
        kwargs: dict[str, Any] = {
            "site_name": [site],
            "search_term": query,
            "location": location or "",
            "results_wanted": limit,
            "country_indeed": country_indeed,
            "proxies": proxies,
            "verbose": False,
        }
        if hours_old is not None:
            kwargs["hours_old"] = hours_old

        df = scrape_jobs(**kwargs)
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

    Default behaviour (set by the API layer):
      - location : USA
      - results  : 20 per source
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

    async def search(
        self,
        query: str,
        location: str | None,
        limit: int,
        hours_old: int | None = 48,
    ) -> list[JobListing]:
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
                hours_old,
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
            listings = _df_to_listings(df, site, max_hours_old=hours_old)
            logger.info("JobSpy[%s] returned %d listings.", site, len(listings))
            all_listings.extend(listings[:limit])

        return all_listings

