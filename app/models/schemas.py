from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, HttpUrl


class JobSource(str, Enum):
    INDEED = "indeed"
    LINKEDIN = "linkedin"
    CAREER_PAGE = "career_page"


class JobListing(BaseModel):
    title: str
    company: Optional[str] = None
    location: Optional[str] = None
    description: Optional[str] = None
    url: Optional[str] = None
    posted_at: Optional[str] = None
    salary: Optional[str] = None
    job_type: Optional[str] = None
    source: JobSource
    scraped_at: datetime = Field(default_factory=datetime.utcnow)


class JobSearchRequest(BaseModel):
    query: str = Field(..., examples=["Python Developer"])
    location: Optional[str] = Field(None, examples=["Remote"])
    limit: int = Field(25, ge=1, le=100)


class JobSearchResponse(BaseModel):
    source: JobSource
    query: str
    location: Optional[str] = None
    count: int
    results: list[JobListing]


class AggregatedSearchResponse(BaseModel):
    query: str
    location: Optional[str] = None
    total_results: int
    results_by_source: dict[str, list[JobListing]]
    errors: dict[str, str] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    status: str
    version: str
