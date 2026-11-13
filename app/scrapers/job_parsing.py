"""Shared parsing for source-specific public job adapters."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from app.models.schemas import JobListing, JobSource

@dataclass
class SourceBatch:
    source: str
    jobs: list[JobListing] = field(default_factory=list)
    status: str = "ok"
    error: str | None = None
    role_hints: dict[str, str] = field(default_factory=dict)
    diagnostics: dict = field(default_factory=dict)


def public_source_url(url: str, root: str) -> bool:
    try:
        parsed = urlsplit(url)
        domain = (urlsplit(root).hostname or "").removeprefix("www.")
        host = parsed.hostname or ""
        return (parsed.scheme == "https" and parsed.port in {None, 443}
                and not parsed.username and (host == domain or host.endswith("." + domain)))
    except ValueError:
        return False


def _postings(value):
    if isinstance(value, list):
        for item in value:
            yield from _postings(item)
    elif isinstance(value, dict):
        kind = value.get("@type")
        if kind == "JobPosting" or isinstance(kind, list) and "JobPosting" in kind:
            yield value
        else:
            for item in value.values():
                if isinstance(item, (dict, list)):
                    yield from _postings(item)


def _plain(value):
    return BeautifulSoup(unescape(str(value or "")), "lxml").get_text(" ", strip=True) or None


def _us_location(value) -> bool:
    return bool(re.search(r"\b(us|usa|united states(?: of america)?)\b", str(value).lower()))


def _worldwide(value) -> bool:
    return str(value).strip().lower() in {"worldwide", "anywhere", "global", "world", "all countries", "anywhere in the world"}


def parse_posting(data: dict, url: str, source: dict) -> JobListing | None:
    title = _plain(data.get("title"))
    if not title:
        return None
    countries, labels = [], []
    locations = data.get("jobLocation") or []
    if isinstance(locations, dict):
        locations = [locations]
    for location in locations:
        address = location.get("address", {}) if isinstance(location, dict) else {}
        if not isinstance(address, dict):
            continue
        country = address.get("addressCountry", "")
        if isinstance(country, dict):
            country = country.get("name") or country.get("identifier") or ""
        countries.append(country)
        labels.append(", ".join(str(value) for value in (
            address.get("addressLocality"), address.get("addressRegion"), country,
        ) if value))
    remote = str(data.get("jobLocationType", "")).upper() == "TELECOMMUTE"
    requirements = data.get("applicantLocationRequirements") or []
    if isinstance(requirements, dict):
        requirements = [requirements]
    permitted = [value.get("name", "") for value in requirements if isinstance(value, dict)]
    if not any(_us_location(country) for country in countries):
        if not remote or not any(_us_location(country) or _worldwide(country) for country in permitted):
            return None
    organization = data.get("hiringOrganization") or {}
    if isinstance(organization, list):
        organization = organization[0] if organization else {}
    job_url = data.get("url")
    job_url = urljoin(url, job_url) if isinstance(job_url, str) else url
    if not public_source_url(job_url, source["url"]):
        job_url = url
    employment = data.get("employmentType")
    if isinstance(employment, list):
        employment = ", ".join(str(item) for item in employment)
    return JobListing(
        title=title, company=organization.get("name") if isinstance(organization, dict) else None,
        location="; ".join(labels or permitted), description=_plain(data.get("description")),
        url=job_url, posted_at=str(data["datePosted"]) if data.get("datePosted") else None,
        valid_through=str(data["validThrough"]) if data.get("validThrough") else None,
        job_type=employment, is_remote=remote,
        source=JobSource(source["id"]), source_url=source["url"],
    )

