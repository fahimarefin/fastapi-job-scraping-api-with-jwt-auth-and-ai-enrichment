"""Provider-specific feeds and the public search APIs used by career sites."""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from lxml import etree

from app.core.config import get_settings
from app.models.schemas import ALL_ROLES_BY_INDUSTRY, JobListing, JobSource
from app.scrapers.job_parsing import SourceBatch, _plain, _postings, _us_location, _worldwide, parse_posting
from app.services.job_selection import matching_roles, parse_date, utcnow
from app.utils.http_client import _build_client, _throttle, fetch


class ProviderError(RuntimeError):
    pass


def date_text(value):
    if value is None:
        return None
    parsed = parse_date(value)
    if not parsed:
        try:
            parsed = parsedate_to_datetime(str(value))
        except (ValueError, TypeError, OverflowError):
            for pattern in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y"):
                try:
                    parsed = datetime.strptime(str(value), pattern).replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    pass
    return parsed.isoformat() if parsed else str(value)


def remote_us(location):
    text = str(location or "")
    return _us_location(text) or _worldwide(text) or bool(re.search(r"\b(?:north america|americas)\b", text, re.I))


def rpc_payload(response):
    response.raise_for_status()
    if "text/event-stream" in response.headers.get("content-type", ""):
        for event in response.text.split("\n\n"):
            lines = [line[5:].lstrip() for line in event.splitlines() if line.startswith("data:")]
            if lines:
                payload = json.loads("\n".join(lines))
                if "result" in payload or "error" in payload:
                    return payload
        raise ProviderError("MCP server returned no JSON-RPC result")
    return response.json()


class ProviderAdapters:
    def __init__(self):
        self.settings = get_settings()

    async def collect(self, source: dict, batch: SourceBatch):
        handler = getattr(self, "_" + source["adapter"], None)
        if handler is None:
            raise ProviderError("Unknown configured source adapter")
        await handler(source, batch)

    def _allowed(self, url, source):
        parsed = urlsplit(url)
        hosts = source.get("allowed_hosts", []) + [urlsplit(source["url"]).hostname]
        if (parsed.scheme != "https" or parsed.port not in {None, 443} or parsed.username
                or not any(parsed.hostname == host or parsed.hostname == (host or "").removeprefix("www.") for host in hosts)):
            raise ProviderError("Provider URL outside configured hosts")
        return parsed.hostname

    async def _get(self, url, source, params=None):
        host = self._allowed(url, source)
        return await fetch(url, params=params, allowed_root="https://" + host)

    async def _post(self, url, source, payload, headers=None):
        self._allowed(url, source)
        await _throttle(url)
        async with _build_client(headers) as client:
            response = await client.post(url, json=payload, follow_redirects=False)
            response.raise_for_status()
            return response

    def _emit(self, source, batch, **values):
        if not values.get("title") or not values.get("url"):
            return
        values["posted_at"] = date_text(values.get("posted_at"))
        values["valid_through"] = date_text(values.get("valid_through"))
        values["description"] = _plain(values.get("description"))
        batch.jobs.append(JobListing(source=JobSource(source["id"]), source_url=source["url"], **values))

    async def _remoteok(self, source, batch):
        payload = (await self._get(source["feed_url"], source)).json()
        if not isinstance(payload, list):
            raise ProviderError("Remote OK feed format changed")
        batch.diagnostics["provider_rows"] = len(payload)
        unknown = []
        for row in payload:
            if not row.get("position"):
                continue
            location = row.get("location")
            if remote_us(location):
                self._emit(source, batch, title=row["position"], company=row.get("company"),
                           location=location, url=row.get("url"), posted_at=row.get("date"),
                           description=row.get("description"), is_remote=True)
            elif not location and matching_roles(row["position"]):
                unknown.append(row.get("url"))
        # Missing location in the feed is checked against the actual job page.
        await self._detail_pages(source, batch, [url for url in unknown if url])

    async def _workingnomads(self, source, batch):
        rows = (await self._get(source["feed_url"], source)).json()
        if not isinstance(rows, list):
            raise ProviderError("Working Nomads feed format changed")
        batch.diagnostics["provider_rows"] = len(rows)
        for row in rows:
            if remote_us(row.get("location")):
                self._emit(source, batch, title=row.get("title"), company=row.get("company_name"),
                           location=row.get("location"), url=row.get("url"), posted_at=row.get("pub_date"),
                           description=row.get("description"), is_remote=True)

    async def _rss(self, source, batch):
        response = await self._get(source["feed_url"], source)
        parser = etree.XMLParser(resolve_entities=False, no_network=True)
        root = etree.fromstring(response.content, parser)
        items = root.findall(".//item")
        batch.diagnostics["provider_rows"] = len(items)
        urls = []
        for item in items:
            url = item.findtext("link")
            raw_title = item.findtext("title") or ""
            company, separator, title = raw_title.partition(": ")
            location = item.findtext("country") or item.findtext("region") or ""
            if remote_us(location):
                self._emit(source, batch, title=title if separator else raw_title,
                           company=company if separator else None, location=location, url=url,
                           description=item.findtext("description"), posted_at=item.findtext("pubDate"),
                           valid_through=item.findtext("expires_at"), job_type=item.findtext("type"), is_remote=True)
            elif not location and url and matching_roles(title if separator else raw_title):
                urls.append(url)
        await self._detail_pages(source, batch, urls[:self.settings.job_adapter_detail_limit])

    async def _himalayas(self, source, batch):
        urls = []
        for page in range(1, self.settings.job_adapter_max_pages + 1):
            payload = (await self._get(source["feed_url"], source, {"country": "US", "sort": "recent", "page": page})).json()
            rows = payload.get("jobs", [])
            if not rows:
                break
            for row in rows:
                guid = row.get("guid")
                if guid and matching_roles(row.get("title", "")):
                    urls.append(guid)
        # pubDate is the provider's update time. Read original datePosted from
        # each job page before admitting it to the 48-hour result.
        await self._detail_pages(source, batch, urls[:self.settings.job_adapter_detail_limit])

    async def _ashby(self, source, batch):
        payload = (await self._get(source["feed_url"], source)).json()
        for row in payload.get("jobs", []):
            address = (row.get("address") or {}).get("postalAddress") or {}
            secondary = row.get("secondaryLocations") or []
            countries = [address.get("addressCountry")] + [
                (location.get("address") or {}).get("addressCountry") for location in secondary]
            if row.get("isListed", True) and any(_us_location(country) for country in countries):
                self._emit(source, batch, title=row.get("title"), company=source["name"],
                           location=row.get("location"), url=row.get("jobUrl"), posted_at=row.get("publishedAt"),
                           description=row.get("descriptionHtml") or row.get("descriptionPlain"),
                           job_type=row.get("employmentType"), is_remote=row.get("isRemote"))

    async def _greenhouse(self, source, batch):
        payload = (await self._get(source["feed_url"], source, {"content": "true"})).json()
        for row in payload.get("jobs", []):
            locations = [row.get("location", {}).get("name", "")] + [office.get("name", "") for office in row.get("offices", [])]
            if any(_us_location(location) for location in locations):
                self._emit(source, batch, title=row.get("title"), company=row.get("company_name") or source["name"],
                           location=row.get("location", {}).get("name"), url=row.get("absolute_url"),
                           posted_at=row.get("first_published"), valid_through=row.get("application_deadline"),
                           description=unescape(row.get("content", "")))

    async def _amazon(self, source, batch):
        for page in range(self.settings.job_adapter_max_pages):
            payload = (await self._get(source["feed_url"], source, {
                "offset": page * 100, "result_limit": 100, "sort": "recent", "country": "USA",
            })).json()
            rows = payload.get("jobs", [])
            if not rows:
                break
            for row in rows:
                if _us_location(row.get("country_code")):
                    self._emit(source, batch, title=row.get("title"), company=row.get("company_name") or "Amazon",
                               location=row.get("location"), url=urljoin(source["url"], row.get("job_path", "")),
                               posted_at=row.get("posted_date"), description=row.get("description"),
                               job_type=row.get("job_schedule_type"))

    async def _jibe(self, source, batch):
        for page in range(1, self.settings.job_adapter_max_pages + 1):
            payload = (await self._get(source["feed_url"], source, {
                "page": page, "limit": 100, "sortBy": "posted_date", "descending": "true",
            })).json()
            rows = payload.get("jobs", [])
            if not rows:
                break
            for item in rows:
                row = item.get("data", {})
                if not _us_location(row.get("country_code") or row.get("country")):
                    continue
                url = row.get("meta_data", {}).get("canonical_url") or source["detail_template"].format(id=row.get("slug"))
                self._emit(source, batch, title=row.get("title"), company=row.get("hiring_organization") or source["name"],
                           location=row.get("full_location"), url=url, posted_at=row.get("posted_date"),
                           description=row.get("description"), job_type=row.get("employment_type"))
            if page * len(rows) >= payload.get("totalCount", 0):
                break

    async def _oracle(self, source, batch):
        for page in range(self.settings.job_adapter_max_pages):
            finder = f'findReqs;siteNumber={source["site"]},limit=100,offset={page * 100},sortBy=POSTING_DATES_DESC'
            payload = (await self._get(source["feed_url"], source, {
                "onlyData": "true", "expand": "requisitionList", "finder": finder,
            })).json()
            items = payload.get("items", [])
            rows = items[0].get("requisitionList", []) if items else []
            if not rows:
                break
            for row in rows:
                country = row.get("PrimaryLocationCountry") or row.get("PrimaryLocationCountryCode")
                if not _us_location(country):
                    continue
                self._emit(source, batch, title=row.get("Title"), company=source["name"],
                           location=row.get("PrimaryLocation"), url=source["detail_template"].format(id=row["Id"]),
                           posted_at=row.get("PostedDate"), valid_through=row.get("PostingEndDate"),
                           description=row.get("ShortDescriptionStr"))
            if (page + 1) * 100 >= items[0].get("TotalJobsCount", 0):
                break

    async def _workday(self, source, batch):
        details = []
        for page in range(self.settings.job_adapter_max_pages):
            response = await self._post(source["feed_url"] + "/jobs", source, {
                "appliedFacets": {}, "limit": 20, "offset": page * 20, "searchText": "",
            })
            payload = response.json()
            rows = payload.get("jobPostings", [])
            if not rows:
                break
            details.extend(row["externalPath"] for row in rows
                           if matching_roles(row.get("title", "")) and row.get("externalPath"))
            if (page + 1) * 20 >= payload.get("total", 0):
                break
        sem = asyncio.Semaphore(3)
        async def detail(path):
            async with sem:
                try:
                    payload = (await self._get(source["feed_url"] + path, source)).json()
                    row = payload.get("jobPostingInfo", {})
                    if _us_location(row.get("country", {}).get("descriptor")):
                        self._emit(source, batch, title=row.get("title"), company=source["name"],
                                   location=row.get("location"), url=row.get("externalUrl") or source["job_base"] + path,
                                   posted_at=row.get("startDate"), valid_through=row.get("endDate"),
                                   description=row.get("jobDescription"), job_type=row.get("timeType"))
                except (httpx.HTTPError, ValueError):
                    batch.diagnostics["detail_failures"] = batch.diagnostics.get("detail_failures", 0) + 1
        async with asyncio.TaskGroup() as group:
            for path in list(dict.fromkeys(details))[:self.settings.job_adapter_detail_limit]:
                group.create_task(detail(path))

    async def _pages(self, source, batch):
        urls = []
        for start in source["search_urls"]:
            response = await self._get(start, source)
            soup = BeautifulSoup(response.text, "lxml")
            self._parse_page(source, batch, soup, str(response.url))
            for anchor in soup.select(source["link_selector"]):
                href = anchor.get("href")
                if href:
                    url = urljoin(str(response.url), href)
                    if re.search(source["job_pattern"], url):
                        urls.append(url)
        batch.diagnostics["detail_links"] = len(set(urls))
        await self._detail_pages(source, batch, list(dict.fromkeys(urls))[:self.settings.job_adapter_detail_limit])
        if not urls and not batch.jobs:
            raise ProviderError("Search page exposed no job links or structured postings")

    def _parse_page(self, source, batch, soup, url):
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(script.string or script.get_text())
            except (ValueError, TypeError):
                continue
            for posting in _postings(data):
                batch.diagnostics["structured_postings"] = batch.diagnostics.get("structured_postings", 0) + 1
                # The job page can be on the company's explicitly verified ATS.
                job = parse_posting(posting, url, {**source, "url": "https://" + urlsplit(url).hostname})
                if job:
                    job.source_url = source["url"]
                    batch.jobs.append(job)

    async def _detail_pages(self, source, batch, urls):
        sem = asyncio.Semaphore(4)
        async def detail(url):
            async with sem:
                try:
                    response = await self._get(url, source)
                    self._parse_page(source, batch, BeautifulSoup(response.text, "lxml"), str(response.url))
                except (httpx.HTTPError, ValueError, ProviderError):
                    batch.diagnostics["detail_failures"] = batch.diagnostics.get("detail_failures", 0) + 1
        async with asyncio.TaskGroup() as group:
            for url in dict.fromkeys(urls):
                group.create_task(detail(url))
        if urls and not batch.jobs and batch.diagnostics.get("detail_failures"):
            batch.status, batch.error = "blocked", "Job detail pages could not be read with the configured access"

    async def _dice_mcp(self, source, batch):
        headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-03-26"}
        init = await self._post(source["feed_url"], source, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "scheduled-jobboard", "version": "1.0"}},
        }, headers)
        payload = rpc_payload(init)
        if "error" in payload:
            raise ProviderError("Dice MCP initialization failed")
        if init.headers.get("mcp-session-id"):
            headers["Mcp-Session-Id"] = init.headers["mcp-session-id"]
        await self._post(source["feed_url"], source, {"jsonrpc": "2.0", "method": "notifications/initialized"}, headers)
        for index, role in enumerate(ALL_ROLES_BY_INDUSTRY["Information Technology"]):
            response = await self._post(source["feed_url"], source, {
                "jsonrpc": "2.0", "id": index + 2, "method": "tools/call",
                "params": {"name": "search_jobs", "arguments": {"keyword": role, "location": "United States",
                    "jobs_per_page": 100, "page_number": 1, "sort": "datePosted", "posted_date": "THREE"}},
            }, headers)
            result = rpc_payload(response).get("result", {})
            if result.get("isError"):
                raise ProviderError("Dice MCP search returned an error")
            payload = result.get("structuredContent")
            if not payload:
                payload = next((json.loads(block["text"]) for block in result.get("content", []) if block.get("type") == "text"), {})
            for row in payload.get("data", []):
                location = row.get("jobLocation")
                if isinstance(location, dict):
                    location = ", ".join(str(value) for value in location.values() if value)
                self._emit(source, batch, title=row.get("title"), company=row.get("companyName"),
                           location=location, url=row.get("detailsPageUrl"), source_company_url=row.get("companyPageUrl"),
                           posted_at=row.get("postedDate"), description=row.get("summary"), salary=row.get("salary"),
                           job_type=row.get("employmentType"), is_remote=row.get("isRemote"))
                if row.get("detailsPageUrl"):
                    batch.role_hints[row["detailsPageUrl"]] = role

    async def _cws(self, source, batch):
        for page in range(self.settings.job_adapter_max_pages):
            response = await self._get(source["feed_url"], source, {
                "Organization": source["organization"], "Limit": 100, "offset": page * 100 + 1,
                "sortfield": "open_date", "sortorder": "descending", "callback": "CWS.jobs.jobCallback",
            })
            raw = response.text.strip()
            if raw.startswith("CWS.jobs.jobCallback("):
                raw = raw[raw.index("(") + 1:raw.rfind(")")]
            payload = json.loads(raw)
            rows = payload.get("queryResult", [])
            if not rows:
                break
            for row in rows:
                if _us_location(row.get("primary_country")):
                    self._emit(source, batch, title=row.get("title"), company=row.get("company_name") or source["name"],
                               location=", ".join(str(value) for value in [row.get("primary_city"), row.get("primary_state"), row.get("primary_country")] if value),
                               url=row.get("url"), posted_at=row.get("open_date"), valid_through=row.get("close_date"),
                               description=row.get("description"))
            if (page + 1) * 100 >= payload.get("totalHits", 0):
                break

    async def _microsoft(self, source, batch):
        start = 0
        for _ in range(self.settings.job_adapter_max_pages):
            payload = (await self._get(source["feed_url"], source, {
                "domain": "microsoft.com", "query": "", "location": "United States", "start": start, "sort_by": "timestamp",
            })).json()
            if payload.get("status") != 200:
                raise ProviderError("Microsoft career search returned an error")
            data = payload.get("data", {})
            rows = data.get("positions", [])
            if not rows:
                break
            for row in rows:
                locations = row.get("locations") or []
                if any(_us_location(location) for location in locations):
                    self._emit(source, batch, title=row.get("name"), company="Microsoft", location="; ".join(locations),
                               url=urljoin(source["job_base"], row.get("positionUrl", "")), posted_at=row.get("postedTs"),
                               is_remote=row.get("workLocationOption") == "remote")
            start += len(rows)
            if start >= data.get("count", 0):
                break
