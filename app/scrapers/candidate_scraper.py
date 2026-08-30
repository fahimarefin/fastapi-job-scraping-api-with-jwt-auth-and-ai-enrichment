# app/scrapers/candidate_scraper.py
"""
Self-hosted LinkedIn candidate scraper — no API keys required.

Strategy:
  1. DuckDuckGo search via `ddgs` library — discover LinkedIn profiles by keyword.
     DDG snippets already contain rich data (name, headline, company).
  2. LinkedIn public profile fetch — attempted for enrichment, but LinkedIn
     blocks most server IPs (returns 999). Gracefully falls back to
     DDG snippet data when blocked.

Dependencies: ddgs, httpx, beautifulsoup4 (all installed).
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from typing import Any

import httpx
from bs4 import BeautifulSoup
from ddgs import DDGS

from app.core.config import get_settings
from app.models.schemas import (
    CandidateEducation,
    CandidateExperience,
    CandidateProfile,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rotating user-agents
# ---------------------------------------------------------------------------

_USER_AGENTS: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]


def _random_ua() -> str:
    return random.choice(_USER_AGENTS)


def _human_delay(base: float = 1.0) -> float:
    """Return a randomised delay to mimic human browsing cadence."""
    return base + random.uniform(0.5, 2.0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_masked(value: Any) -> bool:
    """True if a field is LinkedIn's logged-out asterisk-blur, not real data."""
    if not value:
        return False
    s = str(value)
    letters = sum(c.isalnum() for c in s)
    stars = s.count("*")
    return stars > 0 and stars >= letters


def _safe(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        value = ", ".join(str(v).strip() for v in value if str(v).strip())
    s = str(value).strip()
    return s if s and not _is_masked(s) else None


def _parse_headline_parts(headline: str | None) -> tuple[str | None, str | None]:
    """
    Split a LinkedIn headline like "Senior Dev at Google" into (title, company).
    Common patterns: "Title at Company", "Title @ Company", "Title - Company".
    """
    if not headline:
        return None, None

    for sep in (" at ", " @ ", " - ", " | "):
        if sep in headline:
            parts = headline.split(sep, 1)
            return parts[0].strip(), parts[1].strip()

    return headline.strip(), None


# ---------------------------------------------------------------------------
# DDG result → CandidateProfile parser
# ---------------------------------------------------------------------------

def _parse_ddg_result(result: dict) -> CandidateProfile | None:
    """
    Parse a DuckDuckGo search result dict into a CandidateProfile.

    DDG results look like:
    {
        "title": "Uma Abu - Software Engineer at Netflix | Tech Creator & ...",
        "href": "https://www.linkedin.com/in/umaabu/",
        "body": "View Uma Abu's profile on LinkedIn... · Experience: Netflix · Education: MIT · Location: San Francisco"
    }
    """
    url = result.get("href", "")

    # Only process LinkedIn profile URLs
    if "/in/" not in url or "linkedin.com" not in url:
        return None

    title = result.get("title", "")
    body = result.get("body", "")

    # --- Extract name from title ---
    # Title format: "Name - Headline | LinkedIn" or "Name - Title at Company ..."
    full_name = None
    headline = None

    # Remove trailing " | LinkedIn" or "| LinkedIn"
    clean_title = re.sub(r"\s*\|?\s*LinkedIn\s*$", "", title).strip()

    if " - " in clean_title:
        parts = clean_title.split(" - ", 1)
        full_name = _safe(parts[0])
        headline = _safe(parts[1])
    else:
        full_name = _safe(clean_title)

    # --- Extract structured info from body snippet ---
    location = None
    current_company = None
    education_school = None

    # Body often has patterns like:
    # "Experience: Netflix · Education: MIT · Location: San Francisco"
    # "... Software Engineer at Google. Greater New York City Area. ..."

    # Extract company from "Experience: <Company>"
    exp_match = re.search(r"Experience:\s*(.+?)(?:\s*·|\s*$)", body)
    if exp_match:
        current_company = _safe(exp_match.group(1))

    # Extract location from "Location: <Location>" or "Located in <Location>"
    loc_match = re.search(r"(?:Location|Located in):\s*(.+?)(?:\s*·|\s*$)", body)
    if loc_match:
        location = _safe(loc_match.group(1))

    # Extract education
    edu_match = re.search(r"Education:\s*(.+?)(?:\s*·|\s*$)", body)
    if edu_match:
        education_school = _safe(edu_match.group(1))

    # If no explicit Experience match, try to get company from headline
    current_title, headline_company = _parse_headline_parts(headline)
    if not current_company and headline_company:
        current_company = headline_company

    # Build education list if we found a school
    education: list[CandidateEducation] = []
    if education_school:
        education.append(CandidateEducation(school=education_school))

    # Build experience list from the headline info
    experience: list[CandidateExperience] = []
    if current_title or current_company:
        experience.append(CandidateExperience(
            title=current_title,
            company=current_company,
        ))

    # Extract connections count
    conn_match = re.search(r"(\d+)\+?\s*connections?", body, re.IGNORECASE)
    connections = int(conn_match.group(1)) if conn_match else None

    # Try to extract profile image (not available from DDG, will be None)
    profile_image = None

    return CandidateProfile(
        full_name=full_name,
        headline=headline,
        location=location,
        summary=_safe(body) if body else None,
        profile_url=url.rstrip("/"),
        profile_image_url=profile_image,
        current_company=current_company,
        current_title=current_title,
        skills=[],  # Not available from DDG snippets
        experience=experience,
        education=education,
        connections_count=connections,
    )


# ---------------------------------------------------------------------------
# LinkedIn public profile parser (used when direct fetch succeeds)
# ---------------------------------------------------------------------------

def _parse_jsonld(soup: BeautifulSoup) -> dict | None:
    """Extract JSON-LD Person schema if present."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("@type") == "Person":
                        return item
            elif isinstance(data, dict):
                if data.get("@type") == "Person":
                    return data
                for item in data.get("@graph", []):
                    if isinstance(item, dict) and item.get("@type") == "Person":
                        return item
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def _parse_og_tags(soup: BeautifulSoup) -> dict[str, str]:
    """Extract Open Graph meta tags."""
    og: dict[str, str] = {}
    for meta in soup.find_all("meta", property=True):
        prop = meta.get("property", "")
        if prop.startswith("og:"):
            og[prop] = meta.get("content", "")
    for meta in soup.find_all("meta", attrs={"name": True}):
        name = meta.get("name", "")
        if name in ("description", "title"):
            og[name] = meta.get("content", "")
    return og


def _parse_linkedin_html(html: str, profile_url: str) -> CandidateProfile:
    """
    Parse a LinkedIn public profile page into a CandidateProfile.
    Extracts data from JSON-LD, OG tags, and visible HTML.
    """
    soup = BeautifulSoup(html, "lxml")

    jsonld = _parse_jsonld(soup)
    og = _parse_og_tags(soup)

    full_name = None
    headline = None
    location = None
    summary = None
    profile_image = None
    skills: list[str] = []
    experience: list[CandidateExperience] = []
    education: list[CandidateEducation] = []

    # --- JSON-LD (richest source) ---
    if jsonld:
        full_name = _safe(jsonld.get("name"))
        headline = _safe(jsonld.get("jobTitle") or jsonld.get("headline"))
        addr = jsonld.get("address")
        location = _safe(
            addr.get("addressLocality") if isinstance(addr, dict) else addr
        )
        summary = _safe(jsonld.get("description"))
        img = jsonld.get("image")
        profile_image = _safe(img.get("url") if isinstance(img, dict) else img)

        for org in jsonld.get("alumniOf", []):
            if isinstance(org, dict):
                school = _safe(org.get("name"))
                degree = _safe(org.get("description"))
                if school or degree:
                    education.append(CandidateEducation(school=school, degree=degree))
        for work in jsonld.get("worksFor", []):
            if isinstance(work, dict):
                company = _safe(work.get("name"))
                title = _safe(work.get("jobTitle"))
                if company or title:
                    experience.append(CandidateExperience(company=company, title=title))

    # --- OG tags fallback ---
    if not full_name:
        og_title = og.get("og:title", "")
        if " - " in og_title:
            full_name = _safe(og_title.split(" - ")[0])
        elif og_title and "LinkedIn" not in og_title:
            full_name = _safe(og_title)

    if not summary:
        og_desc = og.get("og:description", "") or og.get("description", "")
        if og_desc:
            summary = _safe(og_desc)
    # (headline is intentionally NOT sourced from og:description — that field is
    # LinkedIn's bio + "Experience: X · Education: Y · Location: Z · N connections"
    # template, never an actual headline. Real headline comes from the <title>
    # tag / top-card selectors below.)

    if not profile_image:
        profile_image = _safe(og.get("og:image"))

    # --- Title tag ---
    title_tag = soup.find("title")
    if title_tag:
        title_text = title_tag.get_text(strip=True)
        parts = [p.strip() for p in title_text.replace(" | LinkedIn", "").split(" - ")]
        if not full_name and parts:
            full_name = _safe(parts[0])
        if not headline and len(parts) > 1:
            headline = _safe(parts[1])

    # --- HTML top card ---
    top_card = soup.select_one(".top-card-layout, .pv-top-card, section.profile")
    if top_card:
        if not full_name:
            name_el = top_card.select_one("h1, .top-card-layout__title")
            if name_el:
                full_name = _safe(name_el.get_text())
        if not headline:
            hl_el = top_card.select_one("h2, .top-card-layout__headline")
            if hl_el:
                headline = _safe(hl_el.get_text())
        if not location:
            loc_el = top_card.select_one(".top-card__subline-item, .top-card-layout__first-subline span")
            if loc_el:
                location = _safe(loc_el.get_text())

    current_title, current_company = _parse_headline_parts(headline)

    return CandidateProfile(
        full_name=full_name,
        headline=headline,
        location=location,
        summary=summary,
        profile_url=profile_url,
        profile_image_url=profile_image,
        current_company=current_company,
        current_title=current_title,
        skills=skills[:20],
        experience=experience[:10],
        education=education[:5],
    )


# ---------------------------------------------------------------------------
# Main scraper class
# ---------------------------------------------------------------------------

class LinkedInCandidateScraper:
    """
    Self-hosted LinkedIn candidate scraper.

    Uses DuckDuckGo (via `ddgs` library) to discover profiles and parses
    the search snippets for structured candidate data. Optionally attempts
    to fetch LinkedIn public profile pages for richer data, but gracefully
    falls back to snippet data when LinkedIn blocks the request (999/403).

    No API keys or paid services required.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self.scrape_delay = settings.candidate_scrape_delay
        self.request_timeout = settings.request_timeout
        self.proxy = settings.http_proxy

    def _build_client(self) -> httpx.AsyncClient:
        """Build an httpx client with browser-like headers."""
        return httpx.AsyncClient(
            timeout=self.request_timeout,
            follow_redirects=True,
            proxy=self.proxy if self.proxy else None,
            headers={
                "User-Agent": _random_ua(),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "DNT": "1",
            },
        )

    # ----- DuckDuckGo search -----

    def _search_ddg(self, query: str, max_results: int = 20) -> list[dict]:
        """
        Search DuckDuckGo using the ddgs library.
        Returns list of result dicts with keys: title, href, body.
        This is synchronous — called via run_in_executor.
        """
        try:
            with DDGS() as ddgs:
                results = list(
                    ddgs.text(
                        query,
                        max_results=max_results,
                        backend="google,brave,duckduckgo,yahoo",
                    )
                )
            return results
        except Exception as exc:
            logger.error("DuckDuckGo search failed: %s", exc)
            raise RuntimeError(f"Search engine request failed: {exc}")

    # ----- LinkedIn profile fetch -----

    async def _fetch_linkedin_profile(self, profile_url: str) -> str | None:
        """
        Attempt to fetch a LinkedIn public profile page.
        Returns HTML or None if blocked (999/403).
        """
        if not profile_url.startswith("https://"):
            profile_url = profile_url.replace("http://", "https://")

        async with self._build_client() as client:
            try:
                resp = await client.get(profile_url)
                if resp.status_code == 200:
                    return resp.text
                elif resp.status_code in (999, 403, 429):
                    logger.debug(
                        "LinkedIn blocked (%d) for %s — using snippet data",
                        resp.status_code, profile_url,
                    )
                    return None
                else:
                    logger.debug("LinkedIn %d for %s", resp.status_code, profile_url)
                    return None
            except httpx.HTTPError as exc:
                logger.debug("Failed to fetch %s: %s", profile_url, exc)
                return None

    # ----- public API -----

    async def lookup_profile(self, linkedin_url: str) -> CandidateProfile | None:
        """
        Fetch and parse a single LinkedIn profile by URL.

        Strategy:
        1. Try direct LinkedIn page fetch → parse HTML
        2. If blocked, search DDG for this specific URL → parse snippet
        """
        logger.info("Looking up profile: %s", linkedin_url)

        # Attempt 1: Direct fetch
        html = await self._fetch_linkedin_profile(linkedin_url)
        if html and len(html) > 5000:  # Real pages are large; auth-walls are tiny
            return _parse_linkedin_html(html, linkedin_url)

        # Attempt 2: DDG search for this specific URL
        logger.info("Direct fetch blocked — searching DDG for %s", linkedin_url)
        loop = asyncio.get_running_loop()
        # Extract username from URL for search query
        username = linkedin_url.rstrip("/").split("/in/")[-1] if "/in/" in linkedin_url else ""
        query = f"linkedin.com/in/{username}"

        try:
            results = await loop.run_in_executor(None, self._search_ddg, query, 5)
        except RuntimeError:
            return None

        # Find the matching result
        clean_url = linkedin_url.rstrip("/")
        for r in results:
            result_url = r.get("href", "").rstrip("/")
            if result_url == clean_url or username in result_url:
                profile = _parse_ddg_result(r)
                if profile:
                    return profile

        # If we got results but none matched exactly, try the first LinkedIn profile
        for r in results:
            if "/in/" in r.get("href", "") and "linkedin.com" in r.get("href", ""):
                profile = _parse_ddg_result(r)
                if profile:
                    return profile

        return None

    async def search_candidates(
        self,
        keyword: str,
        job_title: str | None = None,
        location: str | None = None,
        limit: int = 10,
    ) -> list[CandidateProfile]:

        parts = [f'site:linkedin.com/in'] 
        parts.append(keyword)
        if job_title and job_title != keyword:
            parts.append(job_title)
        if location:
            parts.append(location)
        query = " ".join(parts)

        logger.info("Candidate search query: %s (limit=%d)", query, limit)

        # DDG search — run in executor since ddgs is synchronous
        loop = asyncio.get_running_loop()

        # Request more results than limit since not all will be LinkedIn profiles
        max_results = min(limit * 4, 50)

        try:
            results = await loop.run_in_executor(
                None, self._search_ddg, query, max_results
            )
        except RuntimeError:
            raise

        # Parse LinkedIn profile results
        candidates: list[CandidateProfile] = []
        for r in results:
            if len(candidates) >= limit:
                break

            profile = _parse_ddg_result(r)
            if profile:
                candidates.append(profile)

        logger.info(
            "Candidate search: %d profiles found for '%s' (from %d DDG results)",
            len(candidates), keyword, len(results),
        )

        # Attempt LinkedIn enrichment for each profile (with rate limiting)
        enriched: list[CandidateProfile] = []
        for candidate in candidates:
            if not candidate.profile_url:
                enriched.append(candidate)
                continue

            # Rate limit
            await asyncio.sleep(_human_delay(self.scrape_delay))

            html = await self._fetch_linkedin_profile(candidate.profile_url)
            if html and len(html) > 5000:
                # Merge enriched data with DDG data
                li_profile = _parse_linkedin_html(html, candidate.profile_url)
                # Keep DDG data where LinkedIn data is missing
                enriched.append(CandidateProfile(
                    full_name=li_profile.full_name or candidate.full_name,
                    headline=li_profile.headline or candidate.headline,
                    location=li_profile.location or candidate.location,
                    summary=li_profile.summary or candidate.summary,
                    profile_url=candidate.profile_url,
                    profile_image_url=li_profile.profile_image_url or candidate.profile_image_url,
                    current_company=li_profile.current_company or candidate.current_company,
                    current_title=li_profile.current_title or candidate.current_title,
                    skills=li_profile.skills or candidate.skills,
                    experience=li_profile.experience or candidate.experience,
                    education=li_profile.education or candidate.education,
                    connections_count=li_profile.connections_count or candidate.connections_count,
                ))
                logger.info("Enriched profile from LinkedIn: %s", candidate.profile_url)
            else:
                # Use DDG data as-is
                enriched.append(candidate)

        return enriched
