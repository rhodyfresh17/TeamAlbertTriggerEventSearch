"""Source health monitoring and tracking."""

import json
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any
from enum import Enum


class HealthStatus(Enum):
    """Health status levels for sources."""
    HEALTHY = "healthy"        # Working normally
    DEGRADED = "degraded"      # Occasional failures, still usable
    UNHEALTHY = "unhealthy"    # Frequent failures, may be down
    UNKNOWN = "unknown"        # Not enough data


@dataclass
class SourceHealth:
    """Health metrics for a single source."""
    source_name: str
    source_type: str  # e.g., 'rss_feed', 'job_board', 'api'
    source_url: Optional[str] = None

    # Success/failure tracking
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    consecutive_failures: int = 0
    consecutive_successes: int = 0

    # Timing metrics
    last_check: Optional[float] = None
    last_success: Optional[float] = None
    last_failure: Optional[float] = None
    avg_response_time_ms: float = 0.0
    response_times: List[float] = field(default_factory=list)

    # Event metrics
    events_found_total: int = 0
    events_found_last_run: int = 0

    # Error tracking
    last_error_message: Optional[str] = None
    error_counts: Dict[str, int] = field(default_factory=dict)

    @property
    def success_rate(self) -> float:
        """Calculate success rate."""
        if self.total_requests == 0:
            return 0.0
        return self.successful_requests / self.total_requests

    @property
    def status(self) -> HealthStatus:
        """Determine current health status."""
        if self.total_requests < 3:
            return HealthStatus.UNKNOWN

        # Check consecutive failures
        if self.consecutive_failures >= 3:
            return HealthStatus.UNHEALTHY

        # Check success rate
        if self.success_rate < 0.5:
            return HealthStatus.UNHEALTHY
        elif self.success_rate < 0.9:
            return HealthStatus.DEGRADED

        return HealthStatus.HEALTHY

    @property
    def uptime_percent(self) -> float:
        """Calculate uptime percentage."""
        return self.success_rate * 100

    def record_success(
        self,
        response_time_ms: float = 0.0,
        events_found: int = 0
    ):
        """Record a successful check."""
        now = time.time()
        self.total_requests += 1
        self.successful_requests += 1
        self.consecutive_successes += 1
        self.consecutive_failures = 0
        self.last_check = now
        self.last_success = now
        self.events_found_last_run = events_found
        self.events_found_total += events_found

        # Track response time (keep last 100)
        if response_time_ms > 0:
            self.response_times.append(response_time_ms)
            if len(self.response_times) > 100:
                self.response_times = self.response_times[-100:]
            self.avg_response_time_ms = sum(self.response_times) / len(self.response_times)

    def record_failure(self, error_message: Optional[str] = None):
        """Record a failed check."""
        now = time.time()
        self.total_requests += 1
        self.failed_requests += 1
        self.consecutive_failures += 1
        self.consecutive_successes = 0
        self.last_check = now
        self.last_failure = now
        self.events_found_last_run = 0
        self.last_error_message = error_message

        # Track error types
        if error_message:
            error_type = self._categorize_error(error_message)
            self.error_counts[error_type] = self.error_counts.get(error_type, 0) + 1

    def _categorize_error(self, error_message: str) -> str:
        """Categorize error message into type."""
        error_lower = error_message.lower()
        if 'timeout' in error_lower:
            return 'timeout'
        elif '429' in error_lower or 'rate limit' in error_lower:
            return 'rate_limit'
        elif '403' in error_lower or 'forbidden' in error_lower:
            return 'forbidden'
        elif '404' in error_lower or 'not found' in error_lower:
            return 'not_found'
        elif '500' in error_lower or '502' in error_lower or '503' in error_lower:
            return 'server_error'
        elif 'connection' in error_lower or 'network' in error_lower:
            return 'connection_error'
        elif 'ssl' in error_lower or 'certificate' in error_lower:
            return 'ssl_error'
        elif 'parse' in error_lower or 'xml' in error_lower or 'json' in error_lower:
            return 'parse_error'
        else:
            return 'other'

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            'source_name': self.source_name,
            'source_type': self.source_type,
            'source_url': self.source_url,
            'total_requests': self.total_requests,
            'successful_requests': self.successful_requests,
            'failed_requests': self.failed_requests,
            'consecutive_failures': self.consecutive_failures,
            'consecutive_successes': self.consecutive_successes,
            'last_check': self.last_check,
            'last_success': self.last_success,
            'last_failure': self.last_failure,
            'avg_response_time_ms': self.avg_response_time_ms,
            'events_found_total': self.events_found_total,
            'events_found_last_run': self.events_found_last_run,
            'last_error_message': self.last_error_message,
            'error_counts': self.error_counts,
            'status': self.status.value,
            'success_rate': self.success_rate,
            'uptime_percent': self.uptime_percent,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'SourceHealth':
        """Create from dictionary."""
        # Remove computed fields
        data = dict(data)
        data.pop('status', None)
        data.pop('success_rate', None)
        data.pop('uptime_percent', None)
        data.pop('response_times', None)
        return cls(**data)


class SourceHealthMonitor:
    """
    Monitors health of all data sources.

    Features:
    - Tracks success/failure rates per source
    - Persists health history to disk
    - Provides dashboard-ready metrics
    - Identifies problematic sources
    """

    def __init__(
        self,
        storage_path: str = ".cache/source_health.json",
        auto_save: bool = True
    ):
        self.storage_path = Path(storage_path)
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self.auto_save = auto_save
        self._sources: Dict[str, SourceHealth] = {}
        self._load()

    def _load(self):
        """Load health data from disk."""
        if not self.storage_path.exists():
            return

        try:
            with open(self.storage_path, 'r') as f:
                data = json.load(f)

            for source_name, source_data in data.get('sources', {}).items():
                self._sources[source_name] = SourceHealth.from_dict(source_data)

        except (json.JSONDecodeError, KeyError) as e:
            print(f"Error loading health data: {e}")

    def save(self):
        """Save health data to disk."""
        data = {
            'last_updated': time.time(),
            'sources': {
                name: source.to_dict()
                for name, source in self._sources.items()
            }
        }

        try:
            with open(self.storage_path, 'w') as f:
                json.dump(data, f, indent=2)
        except OSError as e:
            print(f"Error saving health data: {e}")

    def get_or_create(
        self,
        source_name: str,
        source_type: str = 'unknown',
        source_url: Optional[str] = None
    ) -> SourceHealth:
        """Get existing health record or create new one."""
        if source_name not in self._sources:
            self._sources[source_name] = SourceHealth(
                source_name=source_name,
                source_type=source_type,
                source_url=source_url
            )
        return self._sources[source_name]

    def record_check(
        self,
        source_name: str,
        source_type: str,
        success: bool,
        response_time_ms: float = 0.0,
        events_found: int = 0,
        error_message: Optional[str] = None,
        source_url: Optional[str] = None
    ):
        """Record the result of a source check."""
        source = self.get_or_create(source_name, source_type, source_url)

        if success:
            source.record_success(response_time_ms, events_found)
        else:
            source.record_failure(error_message)

        if self.auto_save:
            self.save()

    def get_health(self, source_name: str) -> Optional[SourceHealth]:
        """Get health record for a source."""
        return self._sources.get(source_name)

    def get_all_health(self) -> Dict[str, SourceHealth]:
        """Get health records for all sources."""
        return dict(self._sources)

    def get_unhealthy_sources(self) -> List[SourceHealth]:
        """Get list of unhealthy sources."""
        return [
            source for source in self._sources.values()
            if source.status == HealthStatus.UNHEALTHY
        ]

    def get_degraded_sources(self) -> List[SourceHealth]:
        """Get list of degraded sources."""
        return [
            source for source in self._sources.values()
            if source.status == HealthStatus.DEGRADED
        ]

    def get_summary(self) -> dict:
        """Get overall health summary."""
        sources = list(self._sources.values())

        if not sources:
            return {
                'total_sources': 0,
                'healthy': 0,
                'degraded': 0,
                'unhealthy': 0,
                'unknown': 0,
                'overall_success_rate': 0.0,
                'total_events_found': 0,
            }

        status_counts = {
            HealthStatus.HEALTHY: 0,
            HealthStatus.DEGRADED: 0,
            HealthStatus.UNHEALTHY: 0,
            HealthStatus.UNKNOWN: 0,
        }

        total_requests = 0
        total_successes = 0
        total_events = 0

        for source in sources:
            status_counts[source.status] += 1
            total_requests += source.total_requests
            total_successes += source.successful_requests
            total_events += source.events_found_total

        return {
            'total_sources': len(sources),
            'healthy': status_counts[HealthStatus.HEALTHY],
            'degraded': status_counts[HealthStatus.DEGRADED],
            'unhealthy': status_counts[HealthStatus.UNHEALTHY],
            'unknown': status_counts[HealthStatus.UNKNOWN],
            'overall_success_rate': (
                total_successes / total_requests if total_requests > 0 else 0.0
            ),
            'total_events_found': total_events,
            'last_check': max(
                (s.last_check for s in sources if s.last_check),
                default=None
            ),
        }

    def get_dashboard_data(self) -> dict:
        """Get data formatted for dashboard display."""
        sources = []
        for name, health in sorted(self._sources.items()):
            sources.append({
                'name': name,
                'type': health.source_type,
                'status': health.status.value,
                'status_icon': self._status_icon(health.status),
                'uptime': f"{health.uptime_percent:.1f}%",
                'last_check': self._format_time(health.last_check),
                'events_last_run': health.events_found_last_run,
                'events_total': health.events_found_total,
                'avg_response_ms': f"{health.avg_response_time_ms:.0f}ms",
                'consecutive_failures': health.consecutive_failures,
                'last_error': health.last_error_message[:50] if health.last_error_message else None,
            })

        return {
            'summary': self.get_summary(),
            'sources': sources,
        }

    def _status_icon(self, status: HealthStatus) -> str:
        """Get emoji icon for status."""
        icons = {
            HealthStatus.HEALTHY: "🟢",
            HealthStatus.DEGRADED: "🟡",
            HealthStatus.UNHEALTHY: "🔴",
            HealthStatus.UNKNOWN: "⚪",
        }
        return icons.get(status, "⚪")

    def _format_time(self, timestamp: Optional[float]) -> str:
        """Format timestamp for display."""
        if not timestamp:
            return "Never"

        now = time.time()
        diff = now - timestamp

        if diff < 60:
            return "Just now"
        elif diff < 3600:
            return f"{int(diff / 60)}m ago"
        elif diff < 86400:
            return f"{int(diff / 3600)}h ago"
        else:
            return f"{int(diff / 86400)}d ago"

    def reset_source(self, source_name: str):
        """Reset health data for a source."""
        if source_name in self._sources:
            source_type = self._sources[source_name].source_type
            source_url = self._sources[source_name].source_url
            self._sources[source_name] = SourceHealth(
                source_name=source_name,
                source_type=source_type,
                source_url=source_url
            )
            if self.auto_save:
                self.save()

    def reset_all(self):
        """Reset all health data."""
        self._sources.clear()
        if self.auto_save:
            self.save()
