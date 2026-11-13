"""Collect each board once per refresh, outside API request handling."""
from __future__ import annotations

import asyncio
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from app.core.config import get_settings
from app.models.schemas import ALL_ROLES, SCRAPE_LIMIT, JobListing, JobSource
from app.scrapers.jobspy_scraper import _df_to_listings, _executor, _scrape_one_site_sync
from app.utils.http_client import fetch
from app.scrapers.job_parsing import (
    SourceBatch, public_source_url, parse_posting, _postings, _plain, _us_location, _worldwide,
)
from app.scrapers.provider_adapters import ProviderAdapters

SOURCES = json.loads((Path(__file__).resolve().parents[1] / "core/job_sources.json").read_text())
ALL_SOURCES = SOURCES + [{"id": "career_page", "name": "Configured company career pages",
                          "url": "configured_career_pages", "adapter": "career_pages"}]
_search_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="job-discovery")



def _discover(source: dict, limit: int) -> list[str]:
    from ddgs import DDGS
    domain = urlsplit(source["url"]).hostname
    results = DDGS(timeout=10).text(f"site:{domain} jobs United States", max_results=limit, timelimit="w")
    return list(dict.fromkeys(row["href"] for row in results
                             if row.get("href") and public_source_url(row["href"], source["url"])))



class SourceCollector:
    def __init__(self):
        self.settings = get_settings()
        self.providers = ProviderAdapters()

    async def collect(self, source: dict) -> SourceBatch:
        batch = SourceBatch(source["id"])
        try:
            async with asyncio.timeout(self.settings.job_source_timeout_seconds):
                if source["adapter"] == "jobspy":
                    await self._jobspy(source, batch)
                elif source["adapter"] == "career_pages":
                    await self._career_pages(batch)
                elif source["adapter"] == "api":
                    await self._api(source, batch)
                elif source["adapter"] == "jsonld_discovery":
                    await self._public_pages(source, batch)
                else:
                    await self.providers.collect(source, batch)
        except TimeoutError:
            batch.status = "partial" if batch.jobs else "error"
            batch.error = "Source time budget reached"
        except Exception as exc:
            import httpx
            if isinstance(exc, httpx.HTTPStatusError):
                batch.diagnostics["http_status"] = exc.response.status_code
                batch.status = "partial" if batch.jobs else "blocked" if exc.response.status_code in {401, 403, 429} else "error"
                batch.error = f"Provider returned HTTP {exc.response.status_code}"
                return batch
            batch.status = "partial" if batch.jobs else "error"
            # HTTP errors may contain keys or proxy credentials in their URLs.
            batch.error = f"Source collection failed ({type(exc).__name__})"
        return batch

    async def _jobspy(self, source, batch):
        site = source["id"]
        proxies = [self.settings.http_proxy] if self.settings.http_proxy else None
        if site in {"glassdoor", "zip_recruiter"} and not proxies:
            batch.status, batch.error = "requires_configuration", "Set HTTP_PROXY"
            return
        sem = asyncio.Semaphore(4)
        failures = []
        async def role_search(role):
            async with sem:
                _, frame, error = await asyncio.get_running_loop().run_in_executor(
                    _executor, _scrape_one_site_sync, site, role, "USA", SCRAPE_LIMIT, "USA", proxies, 48,
                )
                if error:
                    failures.append(role)
                elif frame is not None and not frame.empty:
                    jobs = _df_to_listings(frame, site, max_hours_old=48)
                    for job in jobs:
                        job.source_url = source["url"]
                        if job.url:
                            batch.role_hints[job.url] = role
                    batch.jobs.extend(jobs)
        async with asyncio.TaskGroup() as group:
            for role in ALL_ROLES:
                group.create_task(role_search(role))
        if failures:
            batch.status = "partial" if batch.jobs else "error"
            batch.error = f"{len(failures)} role searches failed"

    async def _career_pages(self, batch):
        from app.scrapers.career_pages import CareerPageScraper, _fetch_all_listings
        configs = CareerPageScraper().configs
        results = await asyncio.gather(*(_fetch_all_listings(config) for config in configs), return_exceptions=True)
        for result in results:
            if isinstance(result, list):
                batch.jobs.extend(job for job in result if _us_location(job.location))
        if not batch.jobs:
            batch.status = "error"
            batch.error = "No readable US postings on configured career pages"

    async def _public_pages(self, source, batch):
        urls = [source["url"]]
        discovery_failed = False
        try:
            discovered = await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(
                _search_pool, _discover, source, self.settings.job_discovery_limit,
            ), timeout=20)
            urls = list(dict.fromkeys(discovered + urls))
        except Exception:
            discovery_failed = True
        sem = asyncio.Semaphore(4)
        parsed_pages, failed_pages = [], []
        async def page(url):
            async with sem:
                try:
                    response = await asyncio.wait_for(fetch(url, allowed_root=source["url"]), timeout=12)
                    soup = BeautifulSoup(response.text, "lxml")
                    for script in soup.select('script[type="application/ld+json"]'):
                        try:
                            payload = json.loads(script.string or script.get_text())
                        except (ValueError, TypeError):
                            continue
                        for data in _postings(payload):
                            parsed_pages.append(url)
                            job = parse_posting(data, url, source)
                            if job:
                                batch.jobs.append(job)
                except Exception:
                    failed_pages.append(url)
        async with asyncio.TaskGroup() as group:
            for url in urls:
                group.create_task(page(url))
        if not parsed_pages:
            batch.status = "error"
            batch.error = "No readable public JobPosting data. A dedicated adapter or authorized access may be required."
        elif failed_pages or discovery_failed:
            batch.status = "partial"
            batch.error = "Some indexed pages could not be discovered or read"

    async def _api(self, source, batch):
        sid = source["id"]
        if sid == "remotive":
            payload = (await fetch("https://remotive.com/api/remote-jobs")).json()
            for row in payload.get("jobs", []):
                location = row.get("candidate_required_location", "")
                if _us_location(location) or _worldwide(location):
                    batch.jobs.append(JobListing(
                        title=row["title"], company=row.get("company_name"), location=location,
                        description=_plain(row.get("description")), url=row["url"],
                        posted_at=row.get("publication_date"), salary=row.get("salary"),
                        job_type=row.get("job_type"), is_remote=True,
                        source=JobSource.REMOTIVE, source_url=source["url"],
                    ))
            return
        if sid == "usajobs" and not (self.settings.usajobs_api_key and self.settings.usajobs_email):
            batch.status, batch.error = "requires_configuration", "Set USAJOBS_API_KEY and USAJOBS_EMAIL"
            return
        if sid == "adzuna" and not (self.settings.adzuna_app_id and self.settings.adzuna_app_key):
            batch.status, batch.error = "requires_configuration", "Set ADZUNA_APP_ID and ADZUNA_APP_KEY"
            return
        for role in ALL_ROLES:
            count_before = len(batch.jobs)
            if sid == "usajobs":
                payload = (await fetch("https://data.usajobs.gov/api/Search", params={
                    "PositionTitle": role, "LocationName": "United States", "ResultsPerPage": 10,
                }, extra_headers={"User-Agent": self.settings.usajobs_email,
                                  "Authorization-Key": self.settings.usajobs_api_key})).json()
                for item in payload.get("SearchResult", {}).get("SearchResultItems", []):
                    row = item["MatchedObjectDescriptor"]
                    batch.jobs.append(JobListing(
                        title=row["PositionTitle"], company=row.get("OrganizationName"),
                        location=row.get("PositionLocationDisplay"), url=row.get("PositionURI"),
                        description=_plain(row.get("QualificationSummary")), posted_at=row.get("PublicationStartDate"),
                        valid_through=row.get("ApplicationCloseDate"),
                        source=JobSource.USAJOBS, source_url=source["url"],
                    ))
            elif sid == "adzuna":
                payload = (await fetch("https://api.adzuna.com/v1/api/jobs/us/search/1", params={
                    "app_id": self.settings.adzuna_app_id, "app_key": self.settings.adzuna_app_key,
                    "what": role, "results_per_page": 10, "sort_by": "date", "content-type": "application/json",
                })).json()
                for row in payload.get("results", []):
                    batch.jobs.append(JobListing(
                        title=row["title"], company=row.get("company", {}).get("display_name"),
                        location=row.get("location", {}).get("display_name"), url=row.get("redirect_url"),
                        description=_plain(row.get("description")), posted_at=row.get("created"),
                        source=JobSource.ADZUNA, source_url=source["url"],
                    ))
            else:
                raise ValueError("Unsupported API adapter")
            for job in batch.jobs[count_before:]:
                if job.url:
                    batch.role_hints[job.url] = role
