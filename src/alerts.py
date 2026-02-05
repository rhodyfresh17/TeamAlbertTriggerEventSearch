"""Alert notification system for trigger events."""

import json
import smtplib
import os
from abc import ABC, abstractmethod
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
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

                # Priority order: CFO hires first, then funding, then M&A
                type_order = [
                    EventType.CFO_HIRE,
                    EventType.FUNDING,
                    EventType.EXECUTIVE_HIRE,
                    EventType.MERGER_ACQUISITION,
                    EventType.OTHER,
                ]

                for event_type in type_order:
                    if event_type not in by_type:
                        continue
                    type_events = by_type[event_type]
                    f.write(f"\n## {event_type.value.upper().replace('_', ' ')} ({len(type_events)} events)\n")
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


class EmailAlertHandler(AlertHandler):
    """Handler that sends email alerts."""

    def __init__(
        self,
        smtp_server: str,
        smtp_port: int,
        sender_email: str,
        sender_password: str,
        recipient_emails: List[str]
    ):
        self.smtp_server = smtp_server
        self.smtp_port = smtp_port
        self.sender_email = sender_email
        self.sender_password = sender_password
        self.recipient_emails = recipient_emails

    def send_alert(self, event: TriggerEvent) -> bool:
        """Send single email alert."""
        subject = f"[Trigger Alert] {event.event_type.value}: {event.company_name or 'New Event'}"
        body = event.format_alert()
        return self._send_email(subject, body)

    def send_batch_alert(self, events: List[TriggerEvent]) -> bool:
        """Send batch email alert."""
        if not events:
            return True

        subject = f"[Trigger Alert] {len(events)} New Events in Your Territory"

        # Build HTML email
        html = self._build_html_email(events)
        return self._send_email(subject, html, is_html=True)

    def _build_html_email(self, events: List[TriggerEvent]) -> str:
        """Build HTML email body."""
        html = f"""
        <html>
        <head>
            <style>
                body {{ font-family: Arial, sans-serif; }}
                .event {{ border: 1px solid #ddd; padding: 15px; margin: 10px 0; border-radius: 5px; }}
                .cfo-hire {{ border-left: 4px solid #28a745; }}
                .executive-hire {{ border-left: 4px solid #17a2b8; }}
                .merger-acquisition {{ border-left: 4px solid #dc3545; }}
                .funding {{ border-left: 4px solid #ffc107; }}
                .event-title {{ font-size: 16px; font-weight: bold; margin-bottom: 10px; }}
                .event-meta {{ color: #666; font-size: 12px; }}
                .event-company {{ color: #333; font-weight: bold; }}
                h1 {{ color: #333; }}
                h2 {{ color: #666; border-bottom: 1px solid #eee; padding-bottom: 10px; }}
            </style>
        </head>
        <body>
            <h1>Sales Territory Trigger Events</h1>
            <p>Found {len(events)} new events in your territory.</p>
        """

        # Group by type
        by_type: Dict[EventType, List[TriggerEvent]] = {}
        for event in events:
            if event.event_type not in by_type:
                by_type[event.event_type] = []
            by_type[event.event_type].append(event)

        # Priority order: CFO hires first, then funding, then M&A
        type_order = [
            EventType.CFO_HIRE,
            EventType.FUNDING,
            EventType.EXECUTIVE_HIRE,
            EventType.MERGER_ACQUISITION,
            EventType.OTHER,
        ]

        for event_type in type_order:
            if event_type not in by_type:
                continue
            type_events = by_type[event_type]
            html += f"<h2>{event_type.value.replace('_', ' ').title()} ({len(type_events)})</h2>"

            for event in sorted(type_events, key=lambda e: e.relevance_score, reverse=True):
                css_class = event_type.value.replace('_', '-')
                event_id_short = event.id[:8]
                html += f"""
                <div class="event {css_class}">
                    <div class="event-title">
                        <a href="{event.url}">{event.title}</a>
                    </div>
                    <div class="event-company">{event.company_name or 'Unknown Company'}</div>
                    <div class="event-meta">
                        Source: {event.source_name or event.source.value.replace('_', ' ').title()} |
                        Date: {event.published_date.strftime('%Y-%m-%d')} |
                        Relevance: {event.relevance_score:.0f}%
                    </div>
                    {f'<p>{event.description[:200]}...</p>' if event.description else ''}
                    <div class="event-feedback" style="margin-top: 10px; font-size: 12px; color: #666;">
                        Rate: <code>python -m src.main --rate {event_id_short} good</code> or <code>bad</code>
                    </div>
                </div>
                """

        html += "</body></html>"
        return html

    def _send_email(self, subject: str, body: str, is_html: bool = False) -> bool:
        """Send an email."""
        try:
            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            msg['From'] = self.sender_email
            msg['To'] = ', '.join(self.recipient_emails)

            content_type = 'html' if is_html else 'plain'
            msg.attach(MIMEText(body, content_type))

            with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                server.starttls()
                server.login(self.sender_email, self.sender_password)
                server.sendmail(
                    self.sender_email,
                    self.recipient_emails,
                    msg.as_string()
                )

            return True
        except Exception as e:
            print(f"Error sending email: {e}")
            return False


class SlackAlertHandler(AlertHandler):
    """Handler that sends Slack alerts."""

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def send_alert(self, event: TriggerEvent) -> bool:
        """Send single Slack alert."""
        message = self._format_slack_message(event)
        return self._post_to_slack(message)

    def send_batch_alert(self, events: List[TriggerEvent]) -> bool:
        """Send batch Slack alert."""
        if not events:
            return True

        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"🎯 {len(events)} New Trigger Events"
                }
            },
            {"type": "divider"}
        ]

        # Group by type and add summaries
        by_type: Dict[EventType, List[TriggerEvent]] = {}
        for event in events:
            if event.event_type not in by_type:
                by_type[event.event_type] = []
            by_type[event.event_type].append(event)

        type_emojis = {
            EventType.CFO_HIRE: "💼",
            EventType.EXECUTIVE_HIRE: "👔",
            EventType.MERGER_ACQUISITION: "🤝",
            EventType.FUNDING: "💰",
        }

        for event_type, type_events in by_type.items():
            emoji = type_emojis.get(event_type, "📌")
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{emoji} {event_type.value.replace('_', ' ').title()}* ({len(type_events)})"
                }
            })

            # Top 3 events per type
            for event in sorted(type_events, key=lambda e: e.relevance_score, reverse=True)[:3]:
                blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"• <{event.url}|{event.title[:50]}...>\n  _{event.company_name or 'Unknown'}_ | Score: {event.relevance_score:.0f}"
                    }
                })

        return self._post_to_slack({"blocks": blocks})

    def _format_slack_message(self, event: TriggerEvent) -> dict:
        """Format single event as Slack message."""
        type_emojis = {
            EventType.CFO_HIRE: "💼",
            EventType.EXECUTIVE_HIRE: "👔",
            EventType.MERGER_ACQUISITION: "🤝",
            EventType.FUNDING: "💰",
        }
        emoji = type_emojis.get(event.event_type, "📌")

        return {
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"{emoji} {event.event_type.value.replace('_', ' ').title()}"
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*<{event.url}|{event.title}>*"
                    }
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Company:*\n{event.company_name or 'Unknown'}"},
                        {"type": "mrkdwn", "text": f"*Source:*\n{event.source_name or event.source.value.replace('_', ' ').title()}"},
                        {"type": "mrkdwn", "text": f"*Relevance:*\n{event.relevance_score:.0f}%"},
                        {"type": "mrkdwn", "text": f"*Date:*\n{event.published_date.strftime('%Y-%m-%d')}"},
                    ]
                }
            ]
        }

    def _post_to_slack(self, message: dict) -> bool:
        """Post message to Slack webhook."""
        try:
            response = requests.post(
                self.webhook_url,
                json=message,
                headers={'Content-Type': 'application/json'}
            )
            return response.status_code == 200
        except Exception as e:
            print(f"Error posting to Slack: {e}")
            return False


class DesktopAlertHandler(AlertHandler):
    """Handler that shows desktop notifications."""

    def send_alert(self, event: TriggerEvent) -> bool:
        """Show desktop notification."""
        try:
            # Try using OS-specific notification
            title = f"Trigger Event: {event.event_type.value.replace('_', ' ').title()}"
            message = f"{event.company_name or 'New Event'}: {event.title[:50]}"

            # Try different notification methods
            if self._try_notify_send(title, message):
                return True
            if self._try_osascript(title, message):
                return True

            print(f"Desktop notification: {title} - {message}")
            return True
        except Exception as e:
            print(f"Error showing desktop notification: {e}")
            return False

    def send_batch_alert(self, events: List[TriggerEvent]) -> bool:
        """Show batch desktop notification."""
        if not events:
            return True

        title = f"{len(events)} New Trigger Events"
        message = f"CFO: {sum(1 for e in events if e.event_type == EventType.CFO_HIRE)}, M&A: {sum(1 for e in events if e.event_type == EventType.MERGER_ACQUISITION)}"

        return self.send_alert(TriggerEvent(
            id="batch",
            title=message,
            event_type=EventType.OTHER,
            source=events[0].source,
            url="",
            published_date=datetime.now(),
            company_name=title
        ))

    def _try_notify_send(self, title: str, message: str) -> bool:
        """Try Linux notify-send."""
        try:
            os.system(f'notify-send "{title}" "{message}" 2>/dev/null')
            return True
        except Exception:
            return False

    def _try_osascript(self, title: str, message: str) -> bool:
        """Try macOS osascript."""
        try:
            os.system(f'''osascript -e 'display notification "{message}" with title "{title}"' 2>/dev/null''')
            return True
        except Exception:
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

        # Email alerts
        email_config = alerts_config.get('email', {})
        if email_config.get('enabled', False):
            self.handlers.append(EmailAlertHandler(
                smtp_server=email_config.get('smtp_server', 'smtp.gmail.com'),
                smtp_port=email_config.get('smtp_port', 587),
                sender_email=email_config.get('sender_email', ''),
                sender_password=email_config.get('sender_password', ''),
                recipient_emails=email_config.get('recipient_emails', [])
            ))

        # Slack alerts
        slack_config = alerts_config.get('slack', {})
        if slack_config.get('enabled', False) and slack_config.get('webhook_url'):
            self.handlers.append(SlackAlertHandler(
                webhook_url=slack_config.get('webhook_url')
            ))

        # Desktop alerts
        desktop_config = alerts_config.get('desktop', {})
        if desktop_config.get('enabled', True):
            self.handlers.append(DesktopAlertHandler())

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
