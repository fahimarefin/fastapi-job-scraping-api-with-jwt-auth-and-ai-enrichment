from abc import ABC, abstractmethod

from app.models.schemas import JobListing


class BaseScraper(ABC):
    source_name: str

    @abstractmethod
    async def search(
        self,
        query: str,
        location: str | None,
        limit: int,
        hours_old: int | None = None,
    ) -> list[JobListing]:
        raise NotImplementedError