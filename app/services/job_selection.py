"""Normalize collected jobs and build the existing industry/role response."""
from __future__ import annotations

import re
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.models.schemas import (
    ALL_ROLES_BY_INDUSTRY, MAX_TOTAL_JOBS, SCRAPE_LIMIT, JobListing, RoleResult, ScrapeAllResponse,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_date(value) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, timezone.utc)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parse_date(parsed)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def publication_date(job: JobListing) -> datetime | None:
    parsed = parse_date(job.posted_at)
    if parsed:
        return parsed
    raw = (job.posted_at or "").strip().lower()
    relative = re.fullmatch(r"(\d+)\s*(minute|hour|day)s?(?:\s+ago)?", raw)
    if relative:
        return utcnow() - timedelta(**{relative[2] + "s": int(relative[1])})
    if raw in {"today", "just posted", "now"}:
        return utcnow()
    if raw == "yesterday":
        return utcnow() - timedelta(days=1)
    return parse_date(job.posted_date)


def job_key(job: JobListing) -> str:
    """Deduplicate source URLs, ignoring fragments and advertising parameters."""
    parsed = urlsplit(job.url or "")
    params = [(key, value) for key, value in parse_qsl(parsed.query)
              if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}]
    canonical = urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"),
                            urlencode(sorted(params)), ""))
    return sha256(canonical.encode()).hexdigest()


def matching_roles(title: str) -> list[tuple[str, str]]:
    tokens = set(re.findall(r"[a-z0-9]+", title.lower()))
    matches = []
    for industry, roles in ALL_ROLES_BY_INDUSTRY.items():
        for role in roles:
            if set(re.findall(r"[a-z0-9]+", role.lower())) <= tokens:
                matches.append((industry, role))
    if not matches:
        aliases = {
            r"\b(?:software|application) engineer\b": "Software Developer",
            r"\bfront[ -]?end (?:engineer|developer)\b": "Front-End Developer",
            r"\bback[ -]?end (?:engineer|developer)\b": "Back-End Developer",
            r"\bfull[ -]?stack (?:engineer|developer|swe)\b": "Full Stack Developer",
            r"\b(?:rn|registered professional nurse)\b": "Registered Nurse",
            r"\b(?:lpn|lvn|licensed vocational nurse)\b": "Licensed Practical Nurse",
            r"\b(?:np|aprn|advanced practice provider)\b": "Nurse Practitioner",
            r"\b(?:cna|nursing assistant|nurse assistant|patient care assistant|medical assistant)\b": "Healthcare Support Staff",
            r"\b(?:physical therapist|occupational therapist|ct technologist|radiologic technologist)\b": "Allied Health Professional",
        }
        for pattern, role in aliases.items():
            if re.search(pattern, title, re.I):
                matches.extend((industry, role) for industry, roles in ALL_ROLES_BY_INDUSTRY.items() if role in roles)
    return sorted(matches, key=lambda pair: len(pair[1]), reverse=True)


def candidate(job: JobListing, role_hint: str | None = None) -> dict | None:
    published = publication_date(job)
    expiry = parse_date(job.valid_through)
    now = utcnow()
    if published is None or not now - timedelta(hours=48) <= published <= now:
        return None
    if expiry and expiry <= now:
        return None
    if not job.url or urlsplit(job.url).scheme not in {"http", "https"}:
        return None
    matches = matching_roles(job.title)
    if not matches and role_hint:
        matches = [(industry, role_hint) for industry, roles in ALL_ROLES_BY_INDUSTRY.items() if role_hint in roles]
    if not matches:
        return None
    industry, role = matches[0]
    value = job.model_copy(deep=True)
    value.posted_date = published.date().isoformat()
    value.days_old = round((now - published).total_seconds() / 86400, 3)
    value.is_fresh = True
    return {"job_key": job_key(value), "source": value.source.value,
            "industry": industry, "role": role, "published_at": published,
            "expires_at": expiry, "job": value.model_dump(mode="json")}


def select_jobs(rows: list[dict]) -> list[dict]:
    """Round-robin across industries, sources and roles with a global 800 cap."""
    pools = defaultdict(lambda: defaultdict(lambda: defaultdict(deque)))
    now = utcnow()
    for row in sorted(rows, key=lambda item: parse_date(item["published_at"]) or datetime.min.replace(tzinfo=timezone.utc), reverse=True):
        published = parse_date(row["published_at"])
        expiry = parse_date(row.get("expires_at"))
        if published is None or not now - timedelta(hours=48) <= published <= now or (expiry and expiry <= now):
            continue
        pools[row["industry"]][row["source"]][row["role"]].append(row)
    industries = deque(pools)
    sources = {industry: deque(values) for industry, values in pools.items()}
    roles = {(industry, source): deque(values)
             for industry, values in pools.items() for source, values in values.items()}
    selected, seen = [], set()
    counts = defaultdict(int)
    while industries and len(selected) < MAX_TOTAL_JOBS:
        industry = industries.popleft()
        source = sources[industry].popleft()
        role = roles[industry, source].popleft()
        queue = pools[industry][source][role]
        row = queue.popleft()
        key = (industry, source, role)
        if row["job_key"] not in seen:
            seen.add(row["job_key"])
            selected.append(row)
            counts[key] += 1
        if queue and counts[key] < SCRAPE_LIMIT:
            roles[industry, source].append(role)
        if roles[industry, source]:
            sources[industry].append(source)
        if sources[industry]:
            industries.append(industry)
    return selected


def build_response(rows: list[dict], metadata: dict | None) -> ScrapeAllResponse:
    metadata = metadata or {}
    industries = {}
    by_role = defaultdict(list)
    seen = set()
    now = utcnow()
    for row in rows:
        published = parse_date(row["published_at"])
        expiry = parse_date(row.get("expires_at"))
        if published is None or not now - timedelta(hours=48) <= published <= now or (expiry and expiry <= now):
            continue
        if row["job_key"] in seen:
            continue
        seen.add(row["job_key"])
        job = JobListing.model_validate(row["job"])
        job.days_old = round((now - published).total_seconds() / 86400, 3)
        job.is_fresh = True
        by_role[row["industry"], row["role"]].append(job)
    for industry, roles in ALL_ROLES_BY_INDUSTRY.items():
        industries[industry] = [RoleResult(
            role=role, industry=industry, total=len(by_role[industry, role]), jobs=by_role[industry, role],
        ) for role in roles]
    return ScrapeAllResponse(
        total_roles_scraped=sum(len(roles) for roles in industries.values()),
        total_jobs_found=len(seen), industries=industries,
        ready=bool(metadata.get("snapshot_id")),
        last_updated_at=metadata.get("published_at"),
        refresh_status=metadata.get("phase", "warming_up"),
        source_statuses=metadata.get("source_statuses", {}),
        refresh_error=metadata.get("last_error"),
    )
