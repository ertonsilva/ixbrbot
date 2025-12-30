"""
RSS Monitor module for IX.br Status Bot.
Handles fetching and parsing the IX.br status RSS feed with retry logic.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from enum import Enum
from io import BytesIO

import httpx
import feedparser
from dateutil import parser as date_parser
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log
)

from .config import config, logger

# Timeout for HTTP requests (seconds)
HTTP_TIMEOUT = 30


class EventType(Enum):
    """Types of events from IX.br status page."""
    MAINTENANCE = "maintenance"      # Scheduled maintenance windows
    INCIDENT = "incident"            # Problems/incidents
    RESOLVED = "resolved"            # Resolved incidents
    UNKNOWN = "unknown"              # Unclassified events


@dataclass
class StatusEvent:
    """Represents a single status event from the RSS feed."""
    guid: str                        # Unique identifier
    title: str                       # Event title
    description: str                 # Full description
    link: str                        # Link to status page
    published: datetime              # Publication date
    event_type: EventType            # Type of event
    location: Optional[str] = None   # IX.br location (e.g., "Sao Paulo, SP")
    is_resolved: bool = False        # Whether the incident is resolved

    def get_content_hash(self) -> str:
        """
        Generate a hash of the event content for change detection.
        This hash is used to detect if an event was updated.

        Returns:
            SHA256 hash of the relevant content fields
        """
        content = f"{self.title}|{self.description}|{self.event_type.value}"
        return hashlib.sha256(content.encode()).hexdigest()[:32]

    def to_telegram_message(self) -> str:
        """
        Format the event as a Telegram message with proper formatting.

        Returns:
            Formatted message string with HTML formatting
        """
        # Choose prefix based on event type
        type_labels = {
            EventType.MAINTENANCE: "[MANUTENCAO]",
            EventType.INCIDENT: "[INCIDENTE]",
            EventType.RESOLVED: "[RESOLVIDO]",
            EventType.UNKNOWN: "[AVISO]"
        }
        type_label = type_labels.get(self.event_type, "[AVISO]")

        # Format the message
        lines = [
            f"<b>{type_label}</b>",
            "",
            f"<b>{self._escape_html(self.title)}</b>",
            "",
        ]

        if self.location:
            lines.append(f"<b>Local:</b> {self._escape_html(self.location)}")
            lines.append("")

        if self.description:
            # Truncate long descriptions
            desc = self.description
            if len(desc) > 800:
                desc = desc[:800] + "..."
            lines.append(self._escape_html(desc))
            lines.append("")

        if self.link:
            lines.append(f"<b>Detalhes:</b> {self.link}")
            lines.append("")

        # Format publication date in Brazilian format
        date_str = self.published.strftime("%d/%m/%Y as %H:%M (UTC)")
        lines.append(f"<i>Postado em: {date_str}</i>")

        return "\n".join(lines)

    @staticmethod
    def _escape_html(text: str) -> str:
        """Escape HTML special characters for Telegram."""
        return (
            text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )


class RSSFetchError(Exception):
    """Exception raised when RSS feed fetch fails."""
    pass


class RSSMonitor:
    """Monitors the IX.br RSS feed for status updates."""

    def __init__(self, feed_url: Optional[str] = None):
        """
        Initialize the RSS monitor.

        Args:
            feed_url: URL of the RSS feed. If None, uses config.rss_feed_url
        """
        self.feed_url = feed_url or config.rss_feed_url
        self.max_age_days = config.max_message_age_days
        self._last_successful_fetch: Optional[datetime] = None
        self._consecutive_failures = 0

    async def _fetch_feed_async(self) -> feedparser.FeedParserDict:
        """
        Fetch the RSS feed asynchronously with timeout.

        Returns:
            Parsed feed dictionary

        Raises:
            RSSFetchError: If feed fetch fails
        """
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                response = await client.get(self.feed_url)
                response.raise_for_status()

                # Parse the feed content
                feed = feedparser.parse(BytesIO(response.content))

                # Check for parsing errors with no entries
                if feed.bozo and not feed.entries:
                    raise RSSFetchError(
                        f"Feed parsing error: {feed.bozo_exception}"
                    )

                return feed

        except httpx.HTTPStatusError as e:
            raise RSSFetchError(f"HTTP error {e.response.status_code}")
        except httpx.TimeoutException:
            raise RSSFetchError(f"Timeout after {HTTP_TIMEOUT}s")
        except httpx.RequestError as e:
            raise RSSFetchError(f"Request failed: {e}")
        except RSSFetchError:
            raise
        except Exception as e:
            raise RSSFetchError(f"Failed to fetch RSS feed: {e}")

    def _fetch_feed_sync(self) -> feedparser.FeedParserDict:
        """
        Fetch the RSS feed synchronously (for check_feed_status).
        Uses timeout via httpx.

        Returns:
            Parsed feed dictionary
        """
        try:
            with httpx.Client(timeout=HTTP_TIMEOUT) as client:
                response = client.get(self.feed_url)
                response.raise_for_status()
                return feedparser.parse(BytesIO(response.content))
        except Exception as e:
            raise RSSFetchError(f"Failed to fetch RSS feed: {e}")

    async def fetch_events(self) -> list[StatusEvent]:
        """
        Fetch and parse events from the RSS feed asynchronously.

        Retries up to 3 times with exponential backoff.

        Returns:
            List of StatusEvent objects, filtered by age.
            Returns empty list if fetch fails after retries.
        """
        logger.debug("Fetching RSS feed", extra={"url": self.feed_url})

        # Retry logic
        last_error = None
        for attempt in range(3):
            try:
                feed = await self._fetch_feed_async()

                # Reset failure counter on success
                self._consecutive_failures = 0
                self._last_successful_fetch = datetime.now(timezone.utc)

                events = []
                cutoff_date = datetime.now(timezone.utc) - timedelta(
                    days=self.max_age_days
                )

                for entry in feed.entries:
                    event = self._parse_entry(entry)

                    if event and event.published >= cutoff_date:
                        events.append(event)
                    elif event:
                        logger.debug(
                            "Skipping old event",
                            extra={
                                "title": event.title[:50],
                                "published": event.published.isoformat()
                            }
                        )

                logger.info("RSS feed fetched successfully", extra={
                    "events_count": len(events),
                    "total_entries": len(feed.entries)
                })

                return events

            except RSSFetchError as e:
                last_error = e
                wait_time = 4 * (2 ** attempt)  # 4, 8, 16 seconds
                logger.warning("RSS fetch attempt failed", extra={
                    "attempt": attempt + 1,
                    "error": str(e),
                    "retry_in": wait_time
                })
                if attempt < 2:  # Don't sleep after last attempt
                    import asyncio
                    await asyncio.sleep(wait_time)

        # All retries failed
        self._consecutive_failures += 1
        logger.error("RSS fetch failed after retries", extra={
            "error": str(last_error),
            "consecutive_failures": self._consecutive_failures,
            "last_success": self._last_successful_fetch.isoformat()
                if self._last_successful_fetch else None
        })
        return []

    def check_feed_status(self) -> dict:
        """
        Check the status of the RSS feed.
        Uses synchronous request with timeout.

        Returns:
            Dictionary with feed status information
        """
        result = {
            "reachable": False,
            "last_post_date": None,
            "last_post_title": None,
            "total_entries": 0,
            "error": None,
            "last_successful_fetch": self._last_successful_fetch,
            "consecutive_failures": self._consecutive_failures
        }

        try:
            feed = self._fetch_feed_sync()

            if feed.bozo and not feed.entries:
                result["error"] = str(feed.bozo_exception)
                return result

            result["reachable"] = True
            result["total_entries"] = len(feed.entries)

            if feed.entries:
                latest_entry = feed.entries[0]
                result["last_post_title"] = latest_entry.get("title", "Sem titulo")
                result["last_post_date"] = self._parse_date(latest_entry)

            return result

        except RSSFetchError as e:
            result["error"] = str(e)
            return result
        except Exception as e:
            result["error"] = str(e)
            return result

    def _parse_entry(self, entry: dict) -> Optional[StatusEvent]:
        """Parse a single RSS entry into a StatusEvent."""
        try:
            title = entry.get("title", "Sem titulo")
            description = entry.get("description", entry.get("summary", ""))
            link = entry.get("link", "")
            guid = entry.get("id", entry.get("guid", ""))

            if not guid:
                guid = self._generate_guid(title, description)

            published = self._parse_date(entry)
            event_type, is_resolved = self._classify_event(title, description)
            location = self._extract_location(title)

            return StatusEvent(
                guid=guid,
                title=title,
                description=self._clean_description(description),
                link=link,
                published=published,
                event_type=event_type,
                location=location,
                is_resolved=is_resolved
            )

        except Exception as e:
            logger.error("Failed to parse RSS entry", extra={
                "error": str(e),
                "entry_title": entry.get("title", "unknown")
            })
            return None

    def _parse_date(self, entry: dict) -> datetime:
        """Parse the publication date from an RSS entry."""
        date_str = (
            entry.get("published") or
            entry.get("updated") or
            entry.get("created")
        )

        if date_str:
            try:
                parsed = date_parser.parse(date_str)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed
            except Exception:
                pass

        for field in ["published_parsed", "updated_parsed", "created_parsed"]:
            if entry.get(field):
                try:
                    from time import mktime
                    timestamp = mktime(entry[field])
                    return datetime.fromtimestamp(timestamp, tz=timezone.utc)
                except Exception:
                    pass

        logger.warning("Could not parse date for entry, using current time")
        return datetime.now(timezone.utc)

    def _classify_event(
        self,
        title: str,
        description: str
    ) -> tuple[EventType, bool]:
        """Classify the event type based on title and description."""
        text = f"{title} {description}".lower()

        resolved_keywords = [
            "resolvid", "solved", "restored",
            "restabelecid", "normalizado", "normalized"
        ]
        is_resolved = any(kw in text for kw in resolved_keywords)

        if is_resolved:
            return EventType.RESOLVED, True

        maintenance_keywords = [
            "manutencao", "maintenance", "janela",
            "window", "programad", "scheduled"
        ]
        if any(kw in text for kw in maintenance_keywords):
            return EventType.MAINTENANCE, False

        incident_keywords = [
            "indisponibilidade", "unavailability", "problema",
            "problem", "incident", "incidente", "falha",
            "failure", "rompimento", "disruption"
        ]
        if any(kw in text for kw in incident_keywords):
            return EventType.INCIDENT, False

        return EventType.UNKNOWN, False

    def _extract_location(self, title: str) -> Optional[str]:
        """Extract the IX.br location from the event title."""
        if "IX.br" in title:
            parts = title.split("IX.br")
            if len(parts) > 1:
                location = parts[1].strip()
                for sep in [" - ", " – ", " — "]:
                    if sep in location:
                        location = location.split(sep)[0].strip()
                return location if location else None
        return None

    def _clean_description(self, description: str) -> str:
        """Clean HTML tags and extra whitespace from description."""
        import re

        text = re.sub(r"<[^>]+>", "", description)
        text = re.sub(r"\s+", " ", text).strip()

        for sep in ["+++++", "-----", "=====", "*****"]:
            if sep in text:
                text = text.split(sep)[0].strip()

        return text

    def _generate_guid(self, title: str, description: str) -> str:
        """Generate a unique identifier for an entry without a GUID."""
        content = f"{title}|{description}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]
