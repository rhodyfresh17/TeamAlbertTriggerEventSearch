"""Tests for performance and reliability modules."""

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.performance.rate_limiter import (
    RateLimiter,
    RateLimiterConfig,
    RateLimitExceeded,
    SourceRateState,
)
from src.performance.cache import FileCache, CacheManager
from src.performance.source_health import (
    SourceHealth,
    SourceHealthMonitor,
    HealthStatus,
)


class TestRateLimiter(unittest.TestCase):
    """Tests for RateLimiter."""

    def test_basic_rate_limiting(self):
        """Test basic delay between requests."""
        config = RateLimiterConfig(base_delay=0.1, max_retries=3)
        limiter = RateLimiter(config)

        # First request should not be delayed
        delay = limiter.acquire_sync("example.com")
        self.assertEqual(delay, 0.0)

        # Record success to update last_request_time
        limiter.record_success("example.com")

        # Second immediate request should be delayed
        start = time.time()
        limiter.acquire_sync("example.com")
        elapsed = time.time() - start
        self.assertGreaterEqual(elapsed, 0.05)  # Some delay expected

    def test_record_success_resets_failures(self):
        """Test that success resets consecutive failures."""
        limiter = RateLimiter()
        source = "example.com"

        # Record some failures
        limiter.record_failure(source, 500)
        limiter.record_failure(source, 500)
        state = limiter.get_state(source)
        self.assertEqual(state.consecutive_failures, 2)

        # Success should reset
        limiter.record_success(source)
        state = limiter.get_state(source)
        self.assertEqual(state.consecutive_failures, 0)

    def test_exponential_backoff(self):
        """Test exponential backoff on failures."""
        config = RateLimiterConfig(
            base_delay=1.0,
            backoff_multiplier=2.0,
            max_delay=60.0
        )
        limiter = RateLimiter(config)
        source = "example.com"

        # First failure: 1 * 2^1 = 2s backoff
        delay = limiter.record_failure(source, 429)
        self.assertAlmostEqual(delay, 2.0, places=1)

        # Second failure: 1 * 2^2 = 4s backoff
        delay = limiter.record_failure(source, 429)
        self.assertAlmostEqual(delay, 4.0, places=1)

        # Third failure: 1 * 2^3 = 8s backoff
        delay = limiter.record_failure(source, 429)
        self.assertAlmostEqual(delay, 8.0, places=1)

    def test_max_retries_exceeded(self):
        """Test that max retries raises exception."""
        config = RateLimiterConfig(
            base_delay=0.001,  # Very short for testing
            max_retries=2,
            failure_memory=3600.0  # 1 hour memory
        )
        limiter = RateLimiter(config)
        source = "example.com"

        # Fail multiple times to exceed max_retries
        for _ in range(3):
            limiter.record_failure(source, 429)  # Use rate limit status code

        # Verify failures were recorded
        state = limiter.get_state(source)
        self.assertGreaterEqual(state.consecutive_failures, config.max_retries)

        # Verify last_request_time is set (required for failure memory check)
        self.assertGreater(state.last_request_time, 0)

        # Wait for backoff to clear but stay within failure_memory
        time.sleep(0.1)

        # Next acquire should raise because consecutive_failures >= max_retries
        with self.assertRaises(RateLimitExceeded):
            limiter.acquire_sync(source)

    def test_get_source_key(self):
        """Test URL to source key conversion."""
        limiter = RateLimiter()

        self.assertEqual(
            limiter.get_source_key("https://example.com/feed.xml"),
            "example.com"
        )
        self.assertEqual(
            limiter.get_source_key("https://api.example.com:8080/v1/data"),
            "api.example.com:8080"
        )

    def test_stats(self):
        """Test statistics generation."""
        limiter = RateLimiter()
        source = "example.com"

        limiter.record_success(source)
        limiter.record_success(source)
        limiter.record_failure(source, 500)

        stats = limiter.get_stats()
        self.assertIn(source, stats)
        self.assertEqual(stats[source]['total_requests'], 3)
        self.assertEqual(stats[source]['total_failures'], 1)
        self.assertAlmostEqual(stats[source]['failure_rate'], 1/3, places=2)


class TestFileCache(unittest.TestCase):
    """Tests for FileCache."""

    def setUp(self):
        """Create temporary cache directory."""
        self.temp_dir = tempfile.mkdtemp()
        self.cache = FileCache(cache_dir=self.temp_dir, default_ttl=60.0)

    def tearDown(self):
        """Clean up temporary directory."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_set_and_get(self):
        """Test basic set and get operations."""
        self.cache.set("key1", {"data": "value1"})
        result = self.cache.get("key1")
        self.assertEqual(result, {"data": "value1"})

    def test_get_missing_key(self):
        """Test getting a non-existent key."""
        result = self.cache.get("nonexistent")
        self.assertIsNone(result)

    def test_expiration(self):
        """Test that expired entries return None."""
        self.cache.set("expiring", "value", ttl=0.1)
        time.sleep(0.2)
        result = self.cache.get("expiring")
        self.assertIsNone(result)

    def test_delete(self):
        """Test delete operation."""
        self.cache.set("to_delete", "value")
        self.assertTrue(self.cache.delete("to_delete"))
        self.assertIsNone(self.cache.get("to_delete"))
        self.assertFalse(self.cache.delete("nonexistent"))

    def test_clear(self):
        """Test clearing all entries."""
        self.cache.set("key1", "value1")
        self.cache.set("key2", "value2")
        count = self.cache.clear()
        self.assertEqual(count, 2)
        self.assertIsNone(self.cache.get("key1"))
        self.assertIsNone(self.cache.get("key2"))

    def test_stats(self):
        """Test statistics generation."""
        self.cache.set("key1", "value1")
        self.cache.get("key1")  # Hit
        self.cache.get("key1")  # Hit
        self.cache.get("missing")  # Miss

        stats = self.cache.stats()
        self.assertEqual(stats['entries'], 1)
        self.assertEqual(stats['hits'], 2)
        self.assertEqual(stats['misses'], 1)
        self.assertAlmostEqual(stats['hit_rate'], 2/3, places=2)

    def test_cleanup_expired(self):
        """Test cleanup of expired entries."""
        self.cache.set("keep", "value", ttl=60.0)
        self.cache.set("expire1", "value", ttl=0.1)
        self.cache.set("expire2", "value", ttl=0.1)
        time.sleep(0.2)

        count = self.cache.cleanup_expired()
        self.assertEqual(count, 2)
        self.assertIsNotNone(self.cache.get("keep"))


class TestSourceHealth(unittest.TestCase):
    """Tests for SourceHealth."""

    def test_record_success(self):
        """Test recording successful checks."""
        health = SourceHealth(
            source_name="test_feed",
            source_type="rss_feed"
        )

        health.record_success(response_time_ms=150.0, events_found=5)

        self.assertEqual(health.total_requests, 1)
        self.assertEqual(health.successful_requests, 1)
        self.assertEqual(health.consecutive_successes, 1)
        self.assertEqual(health.events_found_last_run, 5)
        self.assertEqual(health.events_found_total, 5)
        self.assertAlmostEqual(health.avg_response_time_ms, 150.0, places=1)

    def test_record_failure(self):
        """Test recording failed checks."""
        health = SourceHealth(
            source_name="test_feed",
            source_type="rss_feed"
        )

        health.record_failure("Connection timeout")

        self.assertEqual(health.total_requests, 1)
        self.assertEqual(health.failed_requests, 1)
        self.assertEqual(health.consecutive_failures, 1)
        self.assertEqual(health.last_error_message, "Connection timeout")
        self.assertEqual(health.error_counts.get('timeout'), 1)

    def test_success_rate(self):
        """Test success rate calculation."""
        health = SourceHealth(
            source_name="test_feed",
            source_type="rss_feed"
        )

        health.record_success()
        health.record_success()
        health.record_failure("error")

        self.assertAlmostEqual(health.success_rate, 2/3, places=2)

    def test_status_unknown(self):
        """Test status is unknown with few requests."""
        health = SourceHealth(
            source_name="test_feed",
            source_type="rss_feed"
        )
        health.record_success()
        self.assertEqual(health.status, HealthStatus.UNKNOWN)

    def test_status_healthy(self):
        """Test healthy status with high success rate."""
        health = SourceHealth(
            source_name="test_feed",
            source_type="rss_feed"
        )
        for _ in range(10):
            health.record_success()
        self.assertEqual(health.status, HealthStatus.HEALTHY)

    def test_status_unhealthy(self):
        """Test unhealthy status with consecutive failures."""
        health = SourceHealth(
            source_name="test_feed",
            source_type="rss_feed"
        )
        for _ in range(5):
            health.record_failure("error")
        self.assertEqual(health.status, HealthStatus.UNHEALTHY)

    def test_serialization(self):
        """Test to_dict and from_dict."""
        health = SourceHealth(
            source_name="test_feed",
            source_type="rss_feed",
            source_url="https://example.com/feed"
        )
        health.record_success(events_found=3)
        health.record_failure("timeout")

        data = health.to_dict()
        restored = SourceHealth.from_dict(data)

        self.assertEqual(restored.source_name, health.source_name)
        self.assertEqual(restored.total_requests, health.total_requests)
        self.assertEqual(restored.events_found_total, health.events_found_total)


class TestSourceHealthMonitor(unittest.TestCase):
    """Tests for SourceHealthMonitor."""

    def setUp(self):
        """Create temporary storage."""
        self.temp_dir = tempfile.mkdtemp()
        self.storage_path = Path(self.temp_dir) / "health.json"
        self.monitor = SourceHealthMonitor(
            storage_path=str(self.storage_path),
            auto_save=False
        )

    def tearDown(self):
        """Clean up temporary directory."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_record_check(self):
        """Test recording checks."""
        self.monitor.record_check(
            source_name="feed1",
            source_type="rss_feed",
            success=True,
            events_found=5
        )

        health = self.monitor.get_health("feed1")
        self.assertIsNotNone(health)
        self.assertEqual(health.successful_requests, 1)
        self.assertEqual(health.events_found_total, 5)

    def test_get_unhealthy_sources(self):
        """Test getting unhealthy sources."""
        # Create healthy source
        for _ in range(5):
            self.monitor.record_check("healthy", "rss", True)

        # Create unhealthy source
        for _ in range(5):
            self.monitor.record_check("unhealthy", "rss", False, error_message="error")

        unhealthy = self.monitor.get_unhealthy_sources()
        self.assertEqual(len(unhealthy), 1)
        self.assertEqual(unhealthy[0].source_name, "unhealthy")

    def test_get_summary(self):
        """Test summary generation."""
        for _ in range(5):
            self.monitor.record_check("feed1", "rss", True, events_found=2)

        for _ in range(5):
            self.monitor.record_check("feed2", "rss", False, error_message="error")

        summary = self.monitor.get_summary()
        self.assertEqual(summary['total_sources'], 2)
        self.assertEqual(summary['healthy'], 1)
        self.assertEqual(summary['unhealthy'], 1)
        self.assertEqual(summary['total_events_found'], 10)

    def test_persistence(self):
        """Test save and load."""
        self.monitor.record_check("feed1", "rss", True, events_found=5)
        self.monitor.save()

        # Create new monitor with same storage
        new_monitor = SourceHealthMonitor(
            storage_path=str(self.storage_path),
            auto_save=False
        )

        health = new_monitor.get_health("feed1")
        self.assertIsNotNone(health)
        self.assertEqual(health.events_found_total, 5)

    def test_dashboard_data(self):
        """Test dashboard data format."""
        self.monitor.record_check("feed1", "rss", True, events_found=3)

        data = self.monitor.get_dashboard_data()
        self.assertIn('summary', data)
        self.assertIn('sources', data)
        self.assertEqual(len(data['sources']), 1)
        self.assertEqual(data['sources'][0]['name'], "feed1")


class TestAsyncRateLimiter(unittest.TestCase):
    """Tests for async rate limiter functionality."""

    def test_async_acquire(self):
        """Test async acquire."""
        async def run_test():
            config = RateLimiterConfig(base_delay=0.1)
            limiter = RateLimiter(config)

            # First should be instant
            await limiter.acquire("test")
            limiter.record_success("test")  # Update last_request_time

            # Second should wait
            start = time.time()
            await limiter.acquire("test")
            elapsed = time.time() - start
            self.assertGreater(elapsed, 0.05)

        asyncio.run(run_test())


class TestCacheManager(unittest.TestCase):
    """Tests for CacheManager."""

    def setUp(self):
        """Create temporary cache directory."""
        self.temp_dir = tempfile.mkdtemp()
        self.manager = CacheManager(
            cache_dir=self.temp_dir,
            redis_config=None  # No Redis in tests
        )

    def tearDown(self):
        """Clean up temporary directory."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_fallback_to_file_cache(self):
        """Test that file cache works when Redis unavailable."""
        self.manager.set("key", "value")
        result = self.manager.get("key")
        self.assertEqual(result, "value")

    def test_stats(self):
        """Test combined stats."""
        self.manager.set("key", "value")
        self.manager.get("key")

        stats = self.manager.stats()
        self.assertEqual(stats['primary'], 'file')
        self.assertIn('file_cache', stats)


if __name__ == '__main__':
    unittest.main()
