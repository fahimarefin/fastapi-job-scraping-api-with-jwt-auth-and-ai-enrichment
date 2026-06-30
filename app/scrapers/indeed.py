import logging

from bs4 import BeautifulSoup

from app.models.schemas import JobListing, JobSource
from app.scrapers.base import BaseScraper
from app.utils.http_client import fetch

logger = logging.getLogger(__name__)

INDEED_SEARCH_URL = "https://www.indeed.com/jobs"


class IndeedScraper(BaseScraper):
    source_name = "indeed"

    async def search(self, query: str, location: str | None, limit: int) -> list[JobListing]:
        params = {"q": query, "limit": min(limit, 50)}
        if location:
            params["l"] = location

        try:
            response = await fetch(INDEED_SEARCH_URL, params=params)
        except Exception as exc:  # noqa: BLE001
            logger.error("Indeed request failed: %s", exc)
            raise

        return self._parse(response.text, limit)

    @staticmethod
    def _parse(html: str, limit: int) -> list[JobListing]:
        soup = BeautifulSoup(html, "lxml")
        listings: list[JobListing] = []

        # Indeed frequently changes markup/class names and serves JS-rendered
        # content or CAPTCHA pages to non-browser clients. This selector
        # targets the common server-rendered card structure as a best effort;
        # update it if Indeed's markup changes, or switch to the official
        # Indeed Publisher/XML API for reliable production use.
        cards = soup.select("div.job_seen_beacon") or soup.select("td.resultContent")

        for card in cards[:limit]:
            title_el = card.select_one("h2.jobTitle span") or card.select_one("a.jcs-JobTitle span")
            company_el = card.select_one("span.companyName")
            location_el = card.select_one("div.companyLocation")
            link_el = card.select_one("a.jcs-JobTitle") or card.select_one("h2.jobTitle a")
            salary_el = card.select_one("div.metadata.salary-snippet-container")
            summary_el = card.select_one("div.job-snippet")

            if not title_el:
                continue

            href = link_el.get("href") if link_el else None
            url = f"https://www.indeed.com{href}" if href and href.startswith("/") else href

            listings.append(
                JobListing(
                    title=title_el.get_text(strip=True),
                    company=company_el.get_text(strip=True) if company_el else None,
                    location=location_el.get_text(strip=True) if location_el else None,
                    description=summary_el.get_text(strip=True) if summary_el else None,
                    url=url,
                    salary=salary_el.get_text(strip=True) if salary_el else None,
                    source=JobSource.INDEED,
                )
            )

        return listings
