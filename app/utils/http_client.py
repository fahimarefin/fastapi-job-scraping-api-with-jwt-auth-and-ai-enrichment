import asyncio
import time

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.config import get_settings

settings = get_settings()

_last_request_time = 0.0
_lock = asyncio.Lock()


async def _throttle() -> None:
    """Simple shared throttle so we don't hammer target sites."""
    global _last_request_time
    async with _lock:
        elapsed = time.monotonic() - _last_request_time
        wait = settings.min_request_interval - elapsed
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_time = time.monotonic()


def _build_client(extra_headers: dict | None = None) -> httpx.AsyncClient:
    headers = {
        "User-Agent": settings.default_user_agent,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    if extra_headers:
        headers.update(extra_headers)

    proxies = settings.http_proxy if settings.http_proxy else None
    return httpx.AsyncClient(
        headers=headers,
        timeout=settings.request_timeout,
        follow_redirects=True,
        proxy=proxies,
    )


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
)
async def fetch(url: str, params: dict | None = None, extra_headers: dict | None = None) -> httpx.Response:
    await _throttle()
    async with _build_client(extra_headers) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        return response
