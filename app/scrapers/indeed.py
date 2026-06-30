import logging
import xml.etree.ElementTree as ET

from app.models.schemas import JobListing, JobSource
from app.scrapers.base import BaseScraper
from app.utils.http_client import fetch

logger = logging.getLogger(__name__)

# Indeed's RSS feed is a public XML endpoint — no Cloudflare / bot-detection
INDEED_RSS_URL = "https://rss.indeed.com/rss"


class IndeedScraper(BaseScraper):
    source_name = "indeed"

    async def search(self, query: str, location: str | None, limit: int) -> list[JobListing]:
        params: dict = {"q": query, "limit": min(limit, 50)}
        if location:
            params["l"] = location

        try:
            response = await fetch(
                INDEED_RSS_URL,
                params=params,
                extra_headers={
                    # RSS consumers don't send browser headers; keep it clean
                    "Accept": "application/rss+xml, application/xml, text/xml, */*;q=0.8",
                },
            )
        except Exception as exc:
            logger.error("Indeed RSS request failed: %s", exc)
            raise

        return self._parse_rss(response.text, limit)

    @staticmethod
    def _parse_rss(xml_text: str, limit: int) -> list[JobListing]:
        listings: list[JobListing] = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            logger.error("Failed to parse Indeed RSS XML: %s", exc)
            return listings

        channel = root.find("channel")
        if channel is None:
            return listings

        for item in list(channel.findall("item"))[:limit]:
            title = _text(item, "title")
            if not title:
                continue

            # <author> holds "email (Company Name)" or just company
            author_raw = _text(item, "author") or ""
            company = author_raw.split("(")[-1].rstrip(")").strip() if "(" in author_raw else author_raw or None

            # <location> is in the Indeed RSS namespace
            location = (
                _text(item, "{https://rss.indeed.com/}location")
                or _text(item, "location")
            )

            description = _text(item, "description")
            url = _text(item, "link")

            listings.append(
                JobListing(
                    title=title,
                    company=company or None,
                    location=location or None,
                    description=description or None,
                    url=url or None,
                    salary=None,  # RSS feed doesn't include salary
                    source=JobSource.INDEED,
                )
            )

        return listings


def _text(element: ET.Element, tag: str) -> str | None:
    child = element.find(tag)
    return child.text.strip() if child is not None and child.text else None