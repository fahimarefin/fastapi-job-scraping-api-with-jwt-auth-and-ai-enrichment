#app/utils/http_client.py
import asyncio
import logging
import time
from urllib.parse import urlparse
import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.core.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-domain throttle — each domain gets its own lock + last-request time.
# This means different domains can be fetched in parallel while still
# respecting a per-domain minimum request interval.
# ---------------------------------------------------------------------------

_domain_locks: dict[str, asyncio.Lock] = {}
_domain_last_request: dict[str, float] = {}
_registry_lock = asyncio.Lock()


def _get_domain(url: str) -> str:
    """Extract the netloc (host) from a URL to use as the throttle key."""
    return urlparse(url).netloc or url


async def _get_domain_lock(domain: str) -> asyncio.Lock:
    """Return (creating if needed) the per-domain lock — thread-safe."""
    async with _registry_lock:
        if domain not in _domain_locks:
            _domain_locks[domain] = asyncio.Lock()
            _domain_last_request[domain] = 0.0
        return _domain_locks[domain]


async def _throttle(url: str) -> None:
    """Wait if necessary to respect min_request_interval for this domain."""
    domain = _get_domain(url)
    lock = await _get_domain_lock(domain)
    async with lock:
        elapsed = time.monotonic() - _domain_last_request.get(domain, 0.0)
        wait = settings.min_request_interval - elapsed
        if wait > 0:
            await asyncio.sleep(wait)
        _domain_last_request[domain] = time.monotonic()


def _build_client(extra_headers: dict | None = None) -> httpx.AsyncClient:
    headers = {
        "User-Agent": settings.default_user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    if extra_headers:
        headers.update(extra_headers)

    proxies = settings.http_proxy if settings.http_proxy else None
    return httpx.AsyncClient(
        headers=headers,
        timeout=settings.request_timeout,
        follow_redirects=True,
        proxy=proxies,
        http2=True,
    )


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return isinstance(exc, (httpx.TransportError, httpx.TimeoutException))


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception(_is_retryable),
    reraise=True,
)
async def fetch(url: str, params: dict | None = None, extra_headers: dict | None = None) -> httpx.Response:
    await _throttle(url)
    async with _build_client(extra_headers) as client:
        response = await client.get(url, params=params)
        if response.status_code == 403:
            logger.warning("403 from %s — likely blocked by bot detection, not a code bug.", url)
        response.raise_for_status()
        return response