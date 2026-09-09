"""Alert notification system for trigger events."""

import json
import os
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

import requests

from .models import TriggerEvent, EventType


class AlertHandler(ABC):
    """Base class for alert handlers."""

    @abstractmethod
    def send_alert(self, event: TriggerEvent) -> bool:
        """Send alert for an event. Returns True if successful."""
        pass

    @abstractmethod
    def send_batch_alert(self, events: List[TriggerEvent]) -> bool:
        """Send batch alert for multiple events."""
        pass


class FileAlertHandler(AlertHandler):
    """Handler that writes alerts to files."""

    def __init__(self, output_dir: str = "alerts"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def send_alert(self, event: TriggerEvent) -> bool:
        """Write single alert to file."""
        try:
            filename = f"alert_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{event.id[:8]}.txt"
            filepath = self.output_dir / filename

            with open(filepath, 'w') as f:
                f.write(event.format_alert())

            return True
        except Exception as e:
            print(f"Error writing alert file: {e}")
            return False

    def send_batch_alert(self, events: List[TriggerEvent]) -> bool:
        """Write batch alert summary to file."""
        if not events:
            return True

        try:
            filename = f"alert_batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            filepath = self.output_dir / filename

            with open(filepath, 'w') as f:
                f.write(f"TRIGGER EVENT ALERT SUMMARY\n")
                f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"Total Events: {len(events)}\n")
                f.write(f"{'='*60}\n\n")

                # Group by event type
                by_type: Dict[EventType, List[TriggerEvent]] = {}
                for event in events:
                    if event.event_type not in by_type:
                        by_type[event.event_type] = []
                    by_type[event.event_type].append(event)

                # Priority order for display
                type_order = [
                    EventType.CFO_HIRE,
                    EventType.FUNDING,
                    EventType.EXECUTIVE_HIRE,
                    EventType.MERGER_ACQUISITION,
                    EventType.STABLE_TARGET,
                    EventType.OTHER,
                ]

                for event_type in type_order:
                    if event_type not in by_type:
                        continue
                    type_events = by_type[event_type]

                    # Custom labels
                    type_labels = {
                        EventType.CFO_HIRE: "CFO HIRES",
                        EventType.FUNDING: "PE/VC FUNDING",
                        EventType.EXECUTIVE_HIRE: "EXECUTIVE HIRES",
                        EventType.MERGER_ACQUISITION: "MERGERS & ACQUISITIONS",
                        EventType.STABLE_TARGET: "TARGET RECOMMENDATIONS",
                        EventType.OTHER: "OTHER EVENTS",
                    }
                    label = type_labels.get(event_type, event_type.value.upper().replace('_', ' '))
                    f.write(f"\n## {label} ({len(type_events)} events)\n")
                    f.write(f"{'-'*40}\n\n")

                    for event in sorted(type_events, key=lambda e: e.relevance_score, reverse=True):
                        f.write(event.format_alert())
                        f.write("\n\n")

            # Also write JSON version
            json_filepath = self.output_dir / filename.replace('.txt', '.json')
            with open(json_filepath, 'w') as f:
                json.dump([e.to_dict() for e in events], f, indent=2, default=str)

            print(f"Alert written to {filepath}")
            return True

        except Exception as e:
            print(f"Error writing batch alert file: {e}")
            return False








class AlertManager:
    """Manages multiple alert handlers."""

    def __init__(self, config: Dict[str, Any]):
        self.handlers: List[AlertHandler] = []
        self._setup_handlers(config)

    def _setup_handlers(self, config: Dict[str, Any]):
        """Set up alert handlers based on config."""
        alerts_config = config.get('alerts', {})

        # File alerts (always enabled as backup)
        file_config = alerts_config.get('file', {})
        if file_config.get('enabled', True):
            output_dir = file_config.get('output_dir', 'alerts')
            self.handlers.append(FileAlertHandler(output_dir))

        # Email alerts REMOVED 2026-09-08 (A.J.): the fleet has ONE notification
        # pathway and it is Mattermost. An `alerts.email` block in config is now
        # inert — nothing reads it. Do not re-add an SMTP handler here.

        # Slack + Desktop handlers REMOVED 2026-09-08 (A.J.), same call as email:
        # the fleet has ONE notification pathway and it is Mattermost. Any
        # `alerts.slack` / `alerts.desktop` block in config is now inert.
        #
        # Desktop mattered more than it looked: its handler defaulted to
        # enabled=True when no `desktop` block existed, so deleting that block
        # during a config tidy would have switched notifications back ON. Removing
        # the handler makes that impossible rather than merely unlikely.

    def send_alerts(self, events: List[TriggerEvent]) -> int:
        """Send alerts for events through all handlers."""
        if not events:
            return 0

        successful = 0
        for handler in self.handlers:
            try:
                if handler.send_batch_alert(events):
                    successful += 1
            except Exception as e:
                print(f"Error with alert handler {type(handler).__name__}: {e}")

        return successful
