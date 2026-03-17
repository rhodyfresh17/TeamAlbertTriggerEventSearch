"""Rate limiting with exponential backoff for HTTP requests."""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, Optional
from collections import defaultdict


class RateLimitExceeded(Exception):
    """Raised when rate limit is exceeded and max retries reached."""
    pass


@dataclass
class SourceRateState:
    """Tracks rate limit state for a single source."""
    last_request_time: float = 0.0
    consecutive_failures: int = 0
    backoff_until: float = 0.0
    total_requests: int = 0
    total_failures: int = 0
    last_status_code: Optional[int] = None


@dataclass
class RateLimiterConfig:
    """Configuration for rate limiter."""
    # Base delay between requests to same source (seconds)
    base_delay: float = 1.0

    # Maximum delay after exponential backoff (seconds)
    max_delay: float = 300.0  # 5 minutes

    # Exponential backoff multiplier
    backoff_multiplier: float = 2.0

    # Maximum consecutive failures before giving up
    max_retries: int = 4

    # Status codes that trigger backoff
    backoff_status_codes: tuple = (429, 503, 502, 500)

    # How long to remember failure history (seconds)
    failure_memory: float = 3600.0  # 1 hour


class RateLimiter:
    """
    Manages rate limiting with exponential backoff per source.

    Features:
    - Per-source rate tracking
    - Exponential backoff on failures (2s, 4s, 8s, 16s...)
    - Automatic recovery after successful requests
    - Thread-safe for sync usage, coroutine-safe for async
    """

    def __init__(self, config: Optional[RateLimiterConfig] = None):
        self.config = config or RateLimiterConfig()
        self._sources: Dict[str, SourceRateState] = defaultdict(SourceRateState)
        self._lock = asyncio.Lock()

    def get_source_key(self, url: str) -> str:
        """Extract source key from URL (domain)."""
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return parsed.netloc or url

    def get_state(self, source: str) -> SourceRateState:
        """Get current state for a source."""
        return self._sources[source]

    def get_all_states(self) -> Dict[str, SourceRateState]:
        """Get states for all tracked sources."""
        return dict(self._sources)

    async def acquire(self, source: str) -> float:
        """
        Acquire permission to make a request to source.
        Returns the delay that was applied.
        Raises RateLimitExceeded if max retries exceeded.
        """
        async with self._lock:
            state = self._sources[source]
            now = time.time()
            total_wait = 0.0

            # Check if we're in a backoff period
            if state.backoff_until > now:
                wait_time = state.backoff_until - now
                await asyncio.sleep(wait_time)
                total_wait += wait_time
                now = time.time()  # Update now after sleeping

            # Check if max retries exceeded (after backoff period)
            if state.consecutive_failures >= self.config.max_retries:
                # Check if failure memory period has passed
                time_since_last = now - state.last_request_time
                if time_since_last < self.config.failure_memory:
                    raise RateLimitExceeded(
                        f"Max retries ({self.config.max_retries}) exceeded for {source}"
                    )
                else:
                    # Reset after memory period
                    state.consecutive_failures = 0

            # Apply base delay since last request
            time_since_last = now - state.last_request_time
            if time_since_last < self.config.base_delay:
                wait_time = self.config.base_delay - time_since_last
                await asyncio.sleep(wait_time)
                total_wait += wait_time

            return total_wait

    def acquire_sync(self, source: str) -> float:
        """Synchronous version of acquire for non-async code."""
        state = self._sources[source]
        now = time.time()
        total_wait = 0.0

        # Check if we're in a backoff period
        if state.backoff_until > now:
            wait_time = state.backoff_until - now
            time.sleep(wait_time)
            total_wait += wait_time
            now = time.time()  # Update now after sleeping

        # Check if max retries exceeded (after backoff period)
        if state.consecutive_failures >= self.config.max_retries:
            time_since_last = now - state.last_request_time
            if time_since_last < self.config.failure_memory:
                raise RateLimitExceeded(
                    f"Max retries ({self.config.max_retries}) exceeded for {source}"
                )
            else:
                state.consecutive_failures = 0

        # Apply base delay since last request
        time_since_last = now - state.last_request_time
        if time_since_last < self.config.base_delay:
            wait_time = self.config.base_delay - time_since_last
            time.sleep(wait_time)
            total_wait += wait_time

        return total_wait

    def record_success(self, source: str):
        """Record a successful request, resetting backoff state."""
        state = self._sources[source]
        state.last_request_time = time.time()
        state.consecutive_failures = 0
        state.backoff_until = 0.0
        state.total_requests += 1
        state.last_status_code = 200

    def record_failure(self, source: str, status_code: Optional[int] = None):
        """Record a failed request, applying exponential backoff."""
        state = self._sources[source]
        now = time.time()

        state.last_request_time = now
        state.total_requests += 1
        state.total_failures += 1
        state.last_status_code = status_code

        # Apply backoff for rate-limit-related errors
        should_backoff = (
            status_code is None or
            status_code in self.config.backoff_status_codes
        )

        if should_backoff:
            state.consecutive_failures += 1

            # Calculate backoff delay: base_delay * (multiplier ^ failures)
            backoff_delay = self.config.base_delay * (
                self.config.backoff_multiplier ** state.consecutive_failures
            )
            backoff_delay = min(backoff_delay, self.config.max_delay)

            state.backoff_until = now + backoff_delay

            return backoff_delay

        return 0.0

    def reset(self, source: Optional[str] = None):
        """Reset rate limit state for a source or all sources."""
        if source:
            if source in self._sources:
                del self._sources[source]
        else:
            self._sources.clear()

    def get_stats(self) -> Dict[str, dict]:
        """Get statistics for all tracked sources."""
        stats = {}
        for source, state in self._sources.items():
            stats[source] = {
                'total_requests': state.total_requests,
                'total_failures': state.total_failures,
                'failure_rate': (
                    state.total_failures / state.total_requests
                    if state.total_requests > 0 else 0.0
                ),
                'consecutive_failures': state.consecutive_failures,
                'in_backoff': state.backoff_until > time.time(),
                'backoff_remaining': max(0, state.backoff_until - time.time()),
            }
        return stats
