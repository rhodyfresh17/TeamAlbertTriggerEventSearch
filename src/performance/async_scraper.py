"""Async scraper base class for parallel source fetching."""

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False

from .rate_limiter import RateLimiter, RateLimitExceeded, RateLimiterConfig
from .source_health import SourceHealthMonitor
from .cache import CacheManager


@dataclass
class FetchResult:
    """Result of an async fetch operation."""
    url: str
    source_name: str
    success: bool
    content: Optional[bytes] = None
    text: Optional[str] = None
    status_code: Optional[int] = None
    response_time_ms: float = 0.0
    error: Optional[str] = None
    from_cache: bool = False


class AsyncScraperBase(ABC):
    """
    Base class for async scrapers with built-in performance features.

    Features:
    - Parallel fetching with aiohttp
    - Per-source rate limiting
    - Health monitoring
    - Response caching
    - Automatic retry with exponential backoff
    """

    def __init__(
        self,
        config: Dict[str, Any],
        rate_limiter: Optional[RateLimiter] = None,
        health_monitor: Optional[SourceHealthMonitor] = None,
        cache_manager: Optional[CacheManager] = None,
        max_concurrent: int = 10,
    ):
        self.config = config
        self.max_concurrent = max_concurrent

        # Initialize or use provided components
        self.rate_limiter = rate_limiter or RateLimiter(
            RateLimiterConfig(
                base_delay=config.get('scraper', {}).get('request_delay', 1.0),
                max_retries=4,
            )
        )
        self.health_monitor = health_monitor or SourceHealthMonitor()
        self.cache_manager = cache_manager

        # HTTP settings
        self.timeout = config.get('scraper', {}).get('timeout', 30)
        self.user_agent = config.get('scraper', {}).get(
            'user_agent',
            'Mozilla/5.0 (compatible; TriggerEventBot/2.0)'
        )

        # Semaphore for limiting concurrency
        self._semaphore: Optional[asyncio.Semaphore] = None

    @abstractmethod
    async def scrape(self) -> List[Any]:
        """Scrape all sources and return results. Override in subclass."""
        pass

    @abstractmethod
    def get_source_type(self) -> str:
        """Return the source type (e.g., 'rss_feed', 'job_board')."""
        pass

    async def _get_semaphore(self) -> asyncio.Semaphore:
        """Get or create the concurrency semaphore."""
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_concurrent)
        return self._semaphore

    def _get_cache_key(self, url: str) -> str:
        """Generate cache key for a URL."""
        return f"fetch:{self.get_source_type()}:{url}"

    async def fetch_url(
        self,
        url: str,
        source_name: str,
        use_cache: bool = False,
        cache_ttl: float = 300.0,  # 5 minutes default
    ) -> FetchResult:
        """
        Fetch a URL with rate limiting, caching, and health tracking.

        Args:
            url: URL to fetch
            source_name: Human-readable name for logging/monitoring
            use_cache: Whether to use/store in cache
            cache_ttl: Cache TTL in seconds

        Returns:
            FetchResult with content or error
        """
        if not AIOHTTP_AVAILABLE:
            return FetchResult(
                url=url,
                source_name=source_name,
                success=False,
                error="aiohttp not installed. Run: pip install aiohttp"
            )

        # Check cache first
        if use_cache and self.cache_manager:
            cache_key = self._get_cache_key(url)
            cached = self.cache_manager.get(cache_key)
            if cached:
                return FetchResult(
                    url=url,
                    source_name=source_name,
                    success=True,
                    text=cached,
                    from_cache=True
                )

        # Get source key for rate limiting
        source_key = self.rate_limiter.get_source_key(url)

        # Acquire rate limit slot
        try:
            await self.rate_limiter.acquire(source_key)
        except RateLimitExceeded as e:
            return FetchResult(
                url=url,
                source_name=source_name,
                success=False,
                error=str(e)
            )

        # Use semaphore to limit concurrency
        semaphore = await self._get_semaphore()
        async with semaphore:
            start_time = time.time()

            try:
                timeout = aiohttp.ClientTimeout(total=self.timeout)
                headers = {'User-Agent': self.user_agent}

                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url, headers=headers) as response:
                        response_time_ms = (time.time() - start_time) * 1000
                        content = await response.read()
                        text = content.decode('utf-8', errors='replace')

                        if response.status == 200:
                            # Record success
                            self.rate_limiter.record_success(source_key)
                            self.health_monitor.record_check(
                                source_name=source_name,
                                source_type=self.get_source_type(),
                                success=True,
                                response_time_ms=response_time_ms,
                                source_url=url
                            )

                            # Cache successful response
                            if use_cache and self.cache_manager:
                                cache_key = self._get_cache_key(url)
                                self.cache_manager.set(cache_key, text, cache_ttl)

                            return FetchResult(
                                url=url,
                                source_name=source_name,
                                success=True,
                                content=content,
                                text=text,
                                status_code=response.status,
                                response_time_ms=response_time_ms
                            )
                        else:
                            # Record failure
                            error_msg = f"HTTP {response.status}"
                            self.rate_limiter.record_failure(source_key, response.status)
                            self.health_monitor.record_check(
                                source_name=source_name,
                                source_type=self.get_source_type(),
                                success=False,
                                error_message=error_msg,
                                source_url=url
                            )

                            return FetchResult(
                                url=url,
                                source_name=source_name,
                                success=False,
                                status_code=response.status,
                                response_time_ms=response_time_ms,
                                error=error_msg
                            )

            except asyncio.TimeoutError:
                error_msg = f"Timeout after {self.timeout}s"
                self.rate_limiter.record_failure(source_key, None)
                self.health_monitor.record_check(
                    source_name=source_name,
                    source_type=self.get_source_type(),
                    success=False,
                    error_message=error_msg,
                    source_url=url
                )
                return FetchResult(
                    url=url,
                    source_name=source_name,
                    success=False,
                    error=error_msg
                )

            except aiohttp.ClientError as e:
                error_msg = f"Client error: {str(e)[:100]}"
                self.rate_limiter.record_failure(source_key, None)
                self.health_monitor.record_check(
                    source_name=source_name,
                    source_type=self.get_source_type(),
                    success=False,
                    error_message=error_msg,
                    source_url=url
                )
                return FetchResult(
                    url=url,
                    source_name=source_name,
                    success=False,
                    error=error_msg
                )

            except Exception as e:
                error_msg = f"Error: {str(e)[:100]}"
                self.rate_limiter.record_failure(source_key, None)
                self.health_monitor.record_check(
                    source_name=source_name,
                    source_type=self.get_source_type(),
                    success=False,
                    error_message=error_msg,
                    source_url=url
                )
                return FetchResult(
                    url=url,
                    source_name=source_name,
                    success=False,
                    error=error_msg
                )

    async def fetch_many(
        self,
        urls: List[Tuple[str, str]],  # List of (url, source_name)
        use_cache: bool = False,
        cache_ttl: float = 300.0,
    ) -> List[FetchResult]:
        """
        Fetch multiple URLs in parallel.

        Args:
            urls: List of (url, source_name) tuples
            use_cache: Whether to use caching
            cache_ttl: Cache TTL in seconds

        Returns:
            List of FetchResults in same order as input
        """
        if not urls:
            return []

        tasks = [
            self.fetch_url(url, name, use_cache, cache_ttl)
            for url, name in urls
        ]

        return await asyncio.gather(*tasks)

    def fetch_sync(
        self,
        url: str,
        source_name: str,
        use_cache: bool = False,
        cache_ttl: float = 300.0,
    ) -> FetchResult:
        """
        Synchronous wrapper for fetch_url.
        Use this when calling from non-async code.
        """
        return asyncio.run(self.fetch_url(url, source_name, use_cache, cache_ttl))

    def fetch_many_sync(
        self,
        urls: List[Tuple[str, str]],
        use_cache: bool = False,
        cache_ttl: float = 300.0,
    ) -> List[FetchResult]:
        """
        Synchronous wrapper for fetch_many.
        Use this when calling from non-async code.
        """
        return asyncio.run(self.fetch_many(urls, use_cache, cache_ttl))


class AsyncRSSScraperMixin:
    """
    Mixin to add async capabilities to existing RSS scrapers.

    Usage:
        class MyRSSScraper(AsyncRSSScraperMixin, RSSScraper):
            pass
    """

    async def scrape_feeds_async(
        self,
        feeds: List[Dict[str, Any]],
        rate_limiter: Optional[RateLimiter] = None,
        health_monitor: Optional[SourceHealthMonitor] = None,
        max_concurrent: int = 10,
    ) -> List[FetchResult]:
        """
        Scrape multiple RSS feeds in parallel.

        Args:
            feeds: List of feed configs with 'url' and 'name' keys
            rate_limiter: Optional rate limiter instance
            health_monitor: Optional health monitor instance
            max_concurrent: Max concurrent requests

        Returns:
            List of FetchResults
        """
        if not AIOHTTP_AVAILABLE:
            print("Warning: aiohttp not available, falling back to sync")
            return []

        # Build URL list
        urls = [
            (feed['url'], feed.get('name', feed['url']))
            for feed in feeds
            if feed.get('enabled', True) and feed.get('url')
        ]

        if not urls:
            return []

        # Create temporary async scraper for fetching
        class TempScraper(AsyncScraperBase):
            def get_source_type(self):
                return 'rss_feed'

            async def scrape(self):
                return []

        scraper = TempScraper(
            config=getattr(self, 'config', {}),
            rate_limiter=rate_limiter,
            health_monitor=health_monitor,
            max_concurrent=max_concurrent,
        )

        return await scraper.fetch_many(urls)


def run_async_scraper(scraper: AsyncScraperBase) -> List[Any]:
    """
    Helper to run an async scraper from synchronous code.

    Usage:
        scraper = MyAsyncScraper(config)
        results = run_async_scraper(scraper)
    """
    return asyncio.run(scraper.scrape())
