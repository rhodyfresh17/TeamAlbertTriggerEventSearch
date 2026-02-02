"""Data models for trigger events."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class EventType(Enum):
    """Types of trigger events to track."""
    EXECUTIVE_HIRE = "executive_hire"
    CFO_HIRE = "cfo_hire"
    MERGER_ACQUISITION = "merger_acquisition"
    FUNDING = "funding"
    EXPANSION = "expansion"
    OTHER = "other"


class EventSource(Enum):
    """Sources where events are discovered."""
    BUSINESS_WIRE = "business_wire"
    PR_NEWSWIRE = "pr_newswire"
    GLOBE_NEWSWIRE = "globe_newswire"
    SEC_EDGAR = "sec_edgar"
    GOOGLE_NEWS = "google_news"
    LINKEDIN = "linkedin"
    OTHER = "other"


@dataclass
class TriggerEvent:
    """Represents a sales trigger event."""
    id: str
    title: str
    event_type: EventType
    source: EventSource
    url: str
    published_date: datetime
    discovered_date: datetime = field(default_factory=datetime.now)

    # Company information
    company_name: Optional[str] = None
    company_location: Optional[str] = None

    # Event details
    description: Optional[str] = None
    person_name: Optional[str] = None
    person_title: Optional[str] = None

    # For M&A events
    acquirer: Optional[str] = None
    target: Optional[str] = None
    deal_value: Optional[str] = None

    # Matching info
    matched_keywords: list = field(default_factory=list)
    matched_regions: list = field(default_factory=list)
    relevance_score: float = 0.0

    # Alert status
    alert_sent: bool = False

    def to_dict(self) -> dict:
        """Convert to dictionary for storage."""
        return {
            'id': self.id,
            'title': self.title,
            'event_type': self.event_type.value,
            'source': self.source.value,
            'url': self.url,
            'published_date': self.published_date.isoformat(),
            'discovered_date': self.discovered_date.isoformat(),
            'company_name': self.company_name,
            'company_location': self.company_location,
            'description': self.description,
            'person_name': self.person_name,
            'person_title': self.person_title,
            'acquirer': self.acquirer,
            'target': self.target,
            'deal_value': self.deal_value,
            'matched_keywords': self.matched_keywords,
            'matched_regions': self.matched_regions,
            'relevance_score': self.relevance_score,
            'alert_sent': self.alert_sent
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'TriggerEvent':
        """Create from dictionary."""
        return cls(
            id=data['id'],
            title=data['title'],
            event_type=EventType(data['event_type']),
            source=EventSource(data['source']),
            url=data['url'],
            published_date=datetime.fromisoformat(data['published_date']),
            discovered_date=datetime.fromisoformat(data['discovered_date']),
            company_name=data.get('company_name'),
            company_location=data.get('company_location'),
            description=data.get('description'),
            person_name=data.get('person_name'),
            person_title=data.get('person_title'),
            acquirer=data.get('acquirer'),
            target=data.get('target'),
            deal_value=data.get('deal_value'),
            matched_keywords=data.get('matched_keywords', []),
            matched_regions=data.get('matched_regions', []),
            relevance_score=data.get('relevance_score', 0.0),
            alert_sent=data.get('alert_sent', False)
        )

    def format_alert(self) -> str:
        """Format event for alert notification."""
        lines = [
            f"{'='*60}",
            f"TRIGGER EVENT: {self.event_type.value.upper().replace('_', ' ')}",
            f"{'='*60}",
            f"",
            f"Title: {self.title}",
            f"Source: {self.source.value}",
            f"Date: {self.published_date.strftime('%Y-%m-%d %H:%M')}",
        ]

        if self.company_name:
            lines.append(f"Company: {self.company_name}")
        if self.company_location:
            lines.append(f"Location: {self.company_location}")
        if self.person_name:
            lines.append(f"Person: {self.person_name}")
        if self.person_title:
            lines.append(f"Title: {self.person_title}")
        if self.acquirer and self.target:
            lines.append(f"Deal: {self.acquirer} acquiring {self.target}")
        if self.deal_value:
            lines.append(f"Value: {self.deal_value}")

        lines.extend([
            f"",
            f"URL: {self.url}",
            f"",
            f"Matched Keywords: {', '.join(self.matched_keywords)}",
            f"Matched Regions: {', '.join(self.matched_regions)}",
            f"Relevance Score: {self.relevance_score:.2f}",
            f"{'='*60}",
        ])

        return '\n'.join(lines)
