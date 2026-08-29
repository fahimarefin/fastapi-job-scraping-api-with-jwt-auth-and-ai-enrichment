#app/scrapers/career_pages.py

import asyncio
import json
import logging
import os
import time
from bs4 import BeautifulSoup

from app.core.config import get_settings
from app.models.schemas import JobListing, JobSource
from app.scrapers.base import BaseScraper
from app.utils.http_client import fetch

logger = logging.getLogger(__name__)
settings = get_settings()

CACHE_TTL: float = 300.0  # seconds — re-fetch pages at most once per 5 min

# module-level cache: url -> (fetched_at_monotonic, all_listings_unfiltered)
_page_cache: dict[str, tuple[float, list[JobListing]]] = {}
# per-url lock prevents thundering-herd when the cache is cold
_page_cache_locks: dict[str, asyncio.Lock] = {}
_locks_lock = asyncio.Lock()  # guards creation of per-url locks


async def _get_url_lock(url: str) -> asyncio.Lock:
    async with _locks_lock:
        if url not in _page_cache_locks:
            _page_cache_locks[url] = asyncio.Lock()
        return _page_cache_locks[url]


async def _fetch_all_listings(cfg: dict) -> list[JobListing]:
    """
    Fetch and parse ALL listings for one career page URL.
    Results are cached for CACHE_TTL seconds — subsequent calls within
    that window return instantly from memory with no HTTP request.
    """
    url = cfg["url"]
    now = time.monotonic()

    # Fast path: cache hit (no lock needed for read)
    cached = _page_cache.get(url)
    if cached and (now - cached[0]) < CACHE_TTL:
        logger.debug("Career page cache hit: %s", url)
        return cached[1]

    # Slow path: fetch under per-url lock (prevents parallel re-fetching)
    url_lock = await _get_url_lock(url)
    async with url_lock:
        # Re-check after acquiring lock (another coroutine may have just fetched)
        cached = _page_cache.get(url)
        if cached and (now - cached[0]) < CACHE_TTL:
            logger.debug("Career page cache hit (post-lock): %s", url)
            return cached[1]

        try:
            response = await fetch(url)
        except Exception as exc:
            logger.error("Career page fetch failed for %s: %s", cfg.get("company"), exc)
            return []

        soup = BeautifulSoup(response.text, "lxml")
        cards = soup.select(cfg["list_selector"])
        listings: list[JobListing] = []

        for card in cards:
            title_el = card.select_one(cfg["title_selector"])
            if not title_el:
                continue

            location_sel = cfg.get("location_selector", "")
            link_sel = cfg.get("link_selector", "")
            link_attr = cfg.get("link_attr", "href")

            location_el = card.select_one(location_sel) if location_sel else None
            link_el = card.select_one(link_sel) if link_sel else None

            listings.append(
                JobListing(
                    title=title_el.get_text(strip=True),
                    company=cfg.get("company"),
                    location=location_el.get_text(strip=True) if location_el else None,
                    url=link_el.get(link_attr) if link_el else url,
                    source=JobSource.CAREER_PAGE,
                )
            )

        _page_cache[url] = (time.monotonic(), listings)
        logger.info("Career page fetched & cached: %s (%d listings)", url, len(listings))
        return listings


class CareerPageScraper(BaseScraper):
    source_name = "career_page"

    def __init__(self) -> None:
        self.configs = self._load_configs()

    @staticmethod
    def _load_configs() -> list[dict]:
        path = settings.career_pages_config_path
        if not os.path.exists(path):
            logger.warning("Career pages config not found at %s", path)
            return []
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    async def search(self, query: str, location: str | None, limit: int) -> list[JobListing]:
        if not self.configs:
            return []

        query_lower = query.lower()
        location_lower = location.lower() if location else None

        # Fetch (or hit cache for) ALL career page sites concurrently
        per_site_all: list[list[JobListing]] = await asyncio.gather(
            *[_fetch_all_listings(cfg) for cfg in self.configs]
        )

        # Client-side filtering — pure CPU, no I/O
        results: list[JobListing] = []
        for site_listings in per_site_all:
            for listing in site_listings:
                if query_lower and query_lower not in listing.title.lower():
                    continue
                if location_lower and listing.location and location_lower not in listing.location.lower():
                    continue
                results.append(listing)
                if len(results) >= limit:
                    return results

        return results