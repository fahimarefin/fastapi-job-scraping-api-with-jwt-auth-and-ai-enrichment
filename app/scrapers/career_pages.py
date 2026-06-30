import json
import logging
import os

from bs4 import BeautifulSoup

from app.core.config import get_settings
from app.models.schemas import JobListing, JobSource
from app.scrapers.base import BaseScraper
from app.utils.http_client import fetch

logger = logging.getLogger(__name__)
settings = get_settings()


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
        results: list[JobListing] = []
        query_lower = query.lower()

        for cfg in self.configs:
            try:
                response = await fetch(cfg["url"])
            except Exception as exc:
                logger.error("Career page fetch failed for %s: %s", cfg.get("company"), exc)
                continue

            soup = BeautifulSoup(response.text, "lxml")
            cards = soup.select(cfg["list_selector"])

            for card in cards:
                title_el = card.select_one(cfg["title_selector"])
                if not title_el:
                    continue
                title_text = title_el.get_text(strip=True)
                if query_lower and query_lower not in title_text.lower():
                    continue

                location_el = card.select_one(cfg.get("location_selector", "")) if cfg.get("location_selector") else None
                link_el = card.select_one(cfg.get("link_selector", "")) if cfg.get("link_selector") else None
                link_attr = cfg.get("link_attr", "href")

                if location and location_el and location.lower() not in location_el.get_text(strip=True).lower():
                    continue

                results.append(
                    JobListing(
                        title=title_text,
                        company=cfg.get("company"),
                        location=location_el.get_text(strip=True) if location_el else None,
                        url=link_el.get(link_attr) if link_el else cfg["url"],
                        source=JobSource.CAREER_PAGE,
                    )
                )

                if len(results) >= limit:
                    return results

        return results