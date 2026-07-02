#app/models/schemas.py
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Scraping constants — nothing is user-configurable
# ---------------------------------------------------------------------------

SCRAPE_LOCATION: str = "USA"
SCRAPE_LIMIT: int = 20          # results requested per role per source


# ---------------------------------------------------------------------------
# Hardcoded role catalogue — 10 industries, all roles pre-defined
# ---------------------------------------------------------------------------

ALL_ROLES_BY_INDUSTRY: dict[str, list[str]] = {
    "Information Technology": [
        "Chief Technology Officer",
        "IT Executive",
        "IT Manager",
        "IT Director",
        "IT Architect",
        "Software Developer",
        "Full Stack Developer",
        "Front-End Developer",
        "Back-End Developer",
        "Mobile Application Developer",
        "Desktop Application Developer",
        "PHP Developer",
        "JavaScript Developer",
        "TypeScript Developer",
        "DevOps Engineer",
        "Cloud Engineer",
        "Cybersecurity Specialist",
        "Network Administrator",
        "Systems Administrator",
        "IT Analyst",
        "QA Testing Specialist",
        "Data Business Intelligence Specialist",
        "Technical Support Specialist",
    ],
    "Medical & Healthcare": [
        "Physician",
        "Doctor",
        "Registered Nurse",
        "Licensed Practical Nurse",
        "Nurse Practitioner",
        "Allied Health Professional",
        "Medical Technologist",
        "Healthcare Administrator",
        "Mental Health Professional",
        "Paramedic",
        "Healthcare Support Staff",
    ],
    "Accounting & Finance": [
        "Accountant",
        "Bookkeeper",
        "Financial Analyst",
        "Controller",
        "Finance Manager",
        "Accounting Manager",
    ],
    "Executive / C-Suite": [
        "Chief Executive Officer",
        "Chief Operating Officer",
        "Chief Financial Officer",
        "Chief Information Officer",
        "Chief Technology Officer",
        "Chief Human Resources Officer",
        "Vice President",
        "Director",
        "Senior Executive",
    ],
    "Business Administration": [
        "Office Manager",
        "Administrative Assistant",
        "Executive Assistant",
        "Receptionist",
        "Front Office Administrator",
        "Back Office Administrator",
        "Office Coordinator",
        "Mailroom Personnel",
        "Clerical Staff",
        "Data Entry Personnel",
    ],
    "Legal Support": [
        "Legal Secretary",
        "Executive Legal Assistant",
        "Legal Administrative Support",
    ],
    "Administrative & Office Support": [
        "Office Manager",
        "Administrative Assistant",
        "Executive Assistant",
        "Receptionist",
        "Office Coordinator",
        "Clerical Staff",
        "Data Entry Personnel",
    ],
    "Customer Service & Front Office": [
        "Customer Service Representative",
        "Front Office Administrator",
        "Front Desk Receptionist",
        "Call Center Agent",
        "Client Support Specialist",
    ],
    "Government (Public Sector)": [
        "Government Administrator",
        "Public Sector Manager",
        "Policy Analyst",
        "Government IT Specialist",
        "Public Health Officer",
    ],
    "Commercial (Private Sector)": [
        "Sales Manager",
        "Business Development Manager",
        "Account Executive",
        "Commercial Director",
        "Regional Manager",
    ],
}

# Flat deduplicated list of every role across all industries
ALL_ROLES: list[str] = list(dict.fromkeys(
    role
    for roles in ALL_ROLES_BY_INDUSTRY.values()
    for role in roles
))


# ---------------------------------------------------------------------------
# Category slug map  (short URL-safe key → verbose industry name)
# Used by GET /api/v1/categories and GET /api/v1/jobs/unprocessed?category=
# ---------------------------------------------------------------------------

CATEGORY_SLUG_MAP: dict[str, str] = {
    "IT":               "Information Technology",
    "healthcare":       "Medical & Healthcare",
    "finance":          "Accounting & Finance",
    "executive":        "Executive / C-Suite",
    "business":         "Business Administration",
    "legal":            "Legal Support",
    "admin":            "Administrative & Office Support",
    "customer-service": "Customer Service & Front Office",
    "government":       "Government (Public Sector)",
    "commercial":       "Commercial (Private Sector)",
}


# ---------------------------------------------------------------------------
# Job board source enum
# ---------------------------------------------------------------------------

from enum import Enum  # noqa: E402  (kept below constants intentionally)


class JobSource(str, Enum):
    INDEED = "indeed"
    LINKEDIN = "linkedin"
    GLASSDOOR = "glassdoor"
    ZIP_RECRUITER = "zip_recruiter"
    CAREER_PAGE = "career_page"


# ---------------------------------------------------------------------------
# API response models — existing
# ---------------------------------------------------------------------------

class JobListing(BaseModel):
    """A single job posting returned by the scraper."""
    title: str
    company: Optional[str] = None
    location: Optional[str] = None
    description: Optional[str] = None
    url: Optional[str] = None
    posted_at: Optional[str] = None
    salary: Optional[str] = None
    job_type: Optional[str] = None
    is_remote: Optional[bool] = None
    source: JobSource
    scraped_at: datetime = Field(default_factory=datetime.utcnow)


class RoleResult(BaseModel):
    """Scraped results for a single role."""
    role: str
    industry: str
    location: str = SCRAPE_LOCATION
    total: int
    jobs: list[JobListing]
    errors: dict[str, str] = Field(default_factory=dict)


class ScrapeAllResponse(BaseModel):
    """Top-level response returned by GET /api/v1/jobs/scrape-all."""
    location: str = SCRAPE_LOCATION
    results_per_role_requested: int = SCRAPE_LIMIT
    total_roles_scraped: int
    total_jobs_found: int
    industries: dict[str, list[RoleResult]]


class HealthResponse(BaseModel):
    status: str
    version: str


# ---------------------------------------------------------------------------
# API response models — category-based endpoints
# ---------------------------------------------------------------------------

class CategoryInfo(BaseModel):
    """A single industry category with its slug and role list."""
    slug: str
    name: str
    role_count: int
    roles: list[str]


class CategoriesResponse(BaseModel):
    """Response for GET /api/v1/categories."""
    total: int
    categories: list[CategoryInfo]


class DuplicateCheckRequest(BaseModel):
    """Request body for POST /api/v1/jobs/check-duplicates."""
    urls: list[str] = Field(..., description="Job URLs to test for duplicates")


class DuplicateCheckResponse(BaseModel):
    """Response for POST /api/v1/jobs/check-duplicates."""
    total_checked: int
    duplicates: list[str]
    new: list[str]


class MarkProcessedRequest(BaseModel):
    """Request body for PATCH /api/v1/jobs/mark-processed."""
    urls: list[str] = Field(..., description="Job URLs to mark as processed")


class MarkProcessedResponse(BaseModel):
    """Response for PATCH /api/v1/jobs/mark-processed."""
    marked: int
    total_processed: int
    message: str


class UnprocessedJobsResponse(BaseModel):
    """Response for GET /api/v1/jobs/unprocessed."""
    category_slug: str
    category_name: str
    location: str = SCRAPE_LOCATION
    total_scraped: int
    total_unprocessed: int
    jobs: list[JobListing]