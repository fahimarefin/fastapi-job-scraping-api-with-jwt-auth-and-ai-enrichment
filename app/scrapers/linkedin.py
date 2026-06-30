import logging

from bs4 import BeautifulSoup

from app.models.schemas import JobListing, JobSource
from app.scrapers.base import BaseScraper
from app.utils.http_client import fetch

logger = logging.getLogger(__name__)

# LinkedIn's unauthenticated "guest" job search endpoint. It's not an
# official/public API, has no SLA, and LinkedIn aggressively rate-limits
# or blocks repeat callers. For production use, the official LinkedIn
# Talent/Jobs API (partner access required) is the supported path.
LINKEDIN_GUEST_SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"


class LinkedInScraper(BaseScraper):
    source_name = "linkedin"

    async def search(self, query: str, location: str | None, limit: int) -> list[JobListing]:
        params = {
            "keywords": query,
            "location": location or "",
            "start": 0,
        }

        try:
            response = await fetch(LINKEDIN_GUEST_SEARCH_URL, params=params)
        except Exception as exc:  # noqa: BLE001
            logger.error("LinkedIn request failed: %s", exc)
            raise

        return self._parse(response.text, limit)

    @staticmethod
    def _parse(html: str, limit: int) -> list[JobListing]:
        soup = BeautifulSoup(html, "lxml")
        listings: list[JobListing] = []

        cards = soup.select("li")

        for card in cards[:limit]:
            title_el = card.select_one("h3.base-search-card__title")
            company_el = card.select_one("h4.base-search-card__subtitle")
            location_el = card.select_one("span.job-search-card__location")
            link_el = card.select_one("a.base-card__full-link")
            time_el = card.select_one("time")

            if not title_el:
                continue

            listings.append(
                JobListing(
                    title=title_el.get_text(strip=True),
                    company=company_el.get_text(strip=True) if company_el else None,
                    location=location_el.get_text(strip=True) if location_el else None,
                    url=link_el.get("href") if link_el else None,
                    posted_at=time_el.get("datetime") if time_el else None,
                    source=JobSource.LINKEDIN,
                )
            )

        return listings
