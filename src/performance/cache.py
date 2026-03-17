"""Caching layer for API responses and scraped data."""

import hashlib
import json
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union


@dataclass
class CacheEntry:
    """A cached value with metadata."""
    value: Any
    created_at: float
    ttl: float  # Time-to-live in seconds
    hits: int = 0

    def is_expired(self) -> bool:
        """Check if this entry has expired."""
        return time.time() > (self.created_at + self.ttl)


class Cache(ABC):
    """Abstract base class for cache implementations."""

    @abstractmethod
    def get(self, key: str) -> Optional[Any]:
        """Get a value from cache. Returns None if not found or expired."""
        pass

    @abstractmethod
    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        """Set a value in cache with optional TTL override."""
        pass

    @abstractmethod
    def delete(self, key: str) -> bool:
        """Delete a key from cache. Returns True if key existed."""
        pass

    @abstractmethod
    def clear(self) -> int:
        """Clear all entries. Returns count of cleared entries."""
        pass

    @abstractmethod
    def stats(self) -> dict:
        """Get cache statistics."""
        pass

    def make_key(self, *parts: str) -> str:
        """Create a cache key from multiple parts."""
        combined = ':'.join(str(p) for p in parts)
        return hashlib.md5(combined.encode()).hexdigest()


class FileCache(Cache):
    """
    File-based cache for persistence across runs.

    Good for:
    - Apollo/ZoomInfo API responses (expensive, slow-changing)
    - Source health history
    - Deduplicated event IDs
    """

    def __init__(
        self,
        cache_dir: Union[str, Path] = ".cache",
        default_ttl: float = 86400.0,  # 24 hours
        max_entries: int = 10000
    ):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.default_ttl = default_ttl
        self.max_entries = max_entries
        self._hits = 0
        self._misses = 0

    def _get_path(self, key: str) -> Path:
        """Get file path for a cache key."""
        # Use first 2 chars as subdirectory for better filesystem performance
        subdir = key[:2] if len(key) >= 2 else "00"
        dir_path = self.cache_dir / subdir
        dir_path.mkdir(exist_ok=True)
        return dir_path / f"{key}.json"

    def get(self, key: str) -> Optional[Any]:
        """Get a value from the file cache."""
        path = self._get_path(key)

        if not path.exists():
            self._misses += 1
            return None

        try:
            with open(path, 'r') as f:
                data = json.load(f)

            entry = CacheEntry(
                value=data['value'],
                created_at=data['created_at'],
                ttl=data['ttl'],
                hits=data.get('hits', 0)
            )

            if entry.is_expired():
                self._misses += 1
                path.unlink(missing_ok=True)
                return None

            # Update hit count
            self._hits += 1
            entry.hits += 1
            with open(path, 'w') as f:
                json.dump({
                    'value': entry.value,
                    'created_at': entry.created_at,
                    'ttl': entry.ttl,
                    'hits': entry.hits
                }, f)

            return entry.value

        except (json.JSONDecodeError, KeyError, OSError):
            self._misses += 1
            return None

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        """Set a value in the file cache."""
        path = self._get_path(key)
        ttl = ttl if ttl is not None else self.default_ttl

        data = {
            'value': value,
            'created_at': time.time(),
            'ttl': ttl,
            'hits': 0
        }

        try:
            with open(path, 'w') as f:
                json.dump(data, f)
        except (OSError, TypeError) as e:
            print(f"Cache write error for {key}: {e}")

    def delete(self, key: str) -> bool:
        """Delete a key from cache."""
        path = self._get_path(key)
        if path.exists():
            path.unlink()
            return True
        return False

    def clear(self) -> int:
        """Clear all cache entries."""
        import shutil
        count = 0

        for subdir in self.cache_dir.iterdir():
            if subdir.is_dir():
                for f in subdir.glob("*.json"):
                    f.unlink()
                    count += 1

        self._hits = 0
        self._misses = 0
        return count

    def cleanup_expired(self) -> int:
        """Remove expired entries. Returns count of removed entries."""
        count = 0
        now = time.time()

        for subdir in self.cache_dir.iterdir():
            if not subdir.is_dir():
                continue

            for path in subdir.glob("*.json"):
                try:
                    with open(path, 'r') as f:
                        data = json.load(f)

                    if now > (data['created_at'] + data['ttl']):
                        path.unlink()
                        count += 1

                except (json.JSONDecodeError, KeyError, OSError):
                    # Remove corrupt entries
                    path.unlink(missing_ok=True)
                    count += 1

        return count

    def stats(self) -> dict:
        """Get cache statistics."""
        total_entries = 0
        total_size = 0
        expired = 0
        now = time.time()

        for subdir in self.cache_dir.iterdir():
            if not subdir.is_dir():
                continue

            for path in subdir.glob("*.json"):
                total_entries += 1
                total_size += path.stat().st_size

                try:
                    with open(path, 'r') as f:
                        data = json.load(f)
                    if now > (data['created_at'] + data['ttl']):
                        expired += 1
                except (json.JSONDecodeError, KeyError, OSError):
                    expired += 1

        total = self._hits + self._misses
        return {
            'entries': total_entries,
            'expired_entries': expired,
            'size_bytes': total_size,
            'size_mb': total_size / (1024 * 1024),
            'hits': self._hits,
            'misses': self._misses,
            'hit_rate': self._hits / total if total > 0 else 0.0,
        }


class RedisCache(Cache):
    """
    Redis-based cache for high-performance caching.

    Requires redis-py: pip install redis
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 6379,
        db: int = 0,
        password: Optional[str] = None,
        prefix: str = "trigger_event:",
        default_ttl: float = 3600.0,  # 1 hour
    ):
        self.prefix = prefix
        self.default_ttl = default_ttl
        self._hits = 0
        self._misses = 0

        try:
            import redis
            self.redis = redis.Redis(
                host=host,
                port=port,
                db=db,
                password=password,
                decode_responses=True,
                socket_connect_timeout=5,
            )
            # Test connection
            self.redis.ping()
            self._connected = True
        except ImportError:
            print("Redis not installed. Install with: pip install redis")
            self.redis = None
            self._connected = False
        except Exception as e:
            print(f"Redis connection failed: {e}")
            self.redis = None
            self._connected = False

    @property
    def connected(self) -> bool:
        """Check if Redis is connected."""
        return self._connected

    def _make_redis_key(self, key: str) -> str:
        """Create a prefixed Redis key."""
        return f"{self.prefix}{key}"

    def get(self, key: str) -> Optional[Any]:
        """Get a value from Redis."""
        if not self.redis:
            return None

        try:
            redis_key = self._make_redis_key(key)
            data = self.redis.get(redis_key)

            if data is None:
                self._misses += 1
                return None

            self._hits += 1
            return json.loads(data)

        except Exception as e:
            print(f"Redis get error: {e}")
            self._misses += 1
            return None

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        """Set a value in Redis with expiration."""
        if not self.redis:
            return

        try:
            redis_key = self._make_redis_key(key)
            ttl = int(ttl if ttl is not None else self.default_ttl)
            self.redis.setex(redis_key, ttl, json.dumps(value))

        except Exception as e:
            print(f"Redis set error: {e}")

    def delete(self, key: str) -> bool:
        """Delete a key from Redis."""
        if not self.redis:
            return False

        try:
            redis_key = self._make_redis_key(key)
            return self.redis.delete(redis_key) > 0
        except Exception:
            return False

    def clear(self) -> int:
        """Clear all entries with our prefix."""
        if not self.redis:
            return 0

        try:
            pattern = f"{self.prefix}*"
            keys = self.redis.keys(pattern)
            if keys:
                return self.redis.delete(*keys)
            return 0
        except Exception:
            return 0

    def stats(self) -> dict:
        """Get cache statistics."""
        total = self._hits + self._misses

        stats = {
            'connected': self._connected,
            'hits': self._hits,
            'misses': self._misses,
            'hit_rate': self._hits / total if total > 0 else 0.0,
        }

        if self.redis and self._connected:
            try:
                pattern = f"{self.prefix}*"
                stats['entries'] = len(self.redis.keys(pattern))
                info = self.redis.info('memory')
                stats['used_memory'] = info.get('used_memory_human', 'unknown')
            except Exception:
                pass

        return stats


class CacheManager:
    """
    Manages multiple cache layers with fallback.

    Tries Redis first (if available), falls back to file cache.
    """

    def __init__(
        self,
        cache_dir: Union[str, Path] = ".cache",
        redis_config: Optional[dict] = None,
        default_ttl: float = 3600.0,
    ):
        self.default_ttl = default_ttl

        # Initialize file cache (always available)
        self.file_cache = FileCache(cache_dir=cache_dir, default_ttl=default_ttl)

        # Initialize Redis cache (optional)
        self.redis_cache = None
        if redis_config:
            self.redis_cache = RedisCache(
                **redis_config,
                default_ttl=default_ttl
            )
            if not self.redis_cache.connected:
                self.redis_cache = None

    @property
    def primary_cache(self) -> Cache:
        """Get the primary (fastest available) cache."""
        if self.redis_cache and self.redis_cache.connected:
            return self.redis_cache
        return self.file_cache

    def get(self, key: str) -> Optional[Any]:
        """Get from cache, trying Redis first."""
        if self.redis_cache and self.redis_cache.connected:
            result = self.redis_cache.get(key)
            if result is not None:
                return result

        return self.file_cache.get(key)

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        """Set in both caches for redundancy."""
        ttl = ttl if ttl is not None else self.default_ttl

        # Always set in file cache for persistence
        self.file_cache.set(key, value, ttl)

        # Also set in Redis if available
        if self.redis_cache and self.redis_cache.connected:
            self.redis_cache.set(key, value, ttl)

    def delete(self, key: str) -> bool:
        """Delete from both caches."""
        deleted = self.file_cache.delete(key)
        if self.redis_cache and self.redis_cache.connected:
            deleted = self.redis_cache.delete(key) or deleted
        return deleted

    def stats(self) -> dict:
        """Get combined statistics."""
        return {
            'file_cache': self.file_cache.stats(),
            'redis_cache': (
                self.redis_cache.stats()
                if self.redis_cache else {'connected': False}
            ),
            'primary': (
                'redis' if self.redis_cache and self.redis_cache.connected
                else 'file'
            ),
        }
