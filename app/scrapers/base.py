from abc import ABC, abstractmethod

from app.models.schemas import JobListing


class BaseScraper(ABC):
    """All site scrapers implement this interface so the API layer
    can treat them interchangeably."""

    source_name: str

    @abstractmethod
    async def search(self, query: str, location: str | None, limit: int) -> list[JobListing]:
        """Return a list of JobListing objects for the given search."""
        raise NotImplementedError
