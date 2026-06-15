"""Podcast service layer: feed ingestion and catalogue queries.

Auth: caller-controlled. The service layer does not enforce auth.
Response: this layer returns plain Python data; HTTP shape is the
view's responsibility.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import feedparser
import httpx
from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone as django_timezone
from django.utils.dateparse import parse_date, parse_datetime

from podcasts.models import Podcast, PodcastEpisode

logger = logging.getLogger("podcasts")


class PodcastIngestionError(Exception):
    """Raised when a feed cannot be fetched or parsed."""


@dataclass(frozen=True)
class IngestionReport:
    """Result of a single feed ingestion pass.

    Attributes:
        podcast_id: ``Podcast.id`` of the show that was refreshed.
        episodes_seen: Count of ``<item>`` / ``<entry>`` elements in
            the feed.
        episodes_created: New rows added to ``PodcastEpisode``.
        episodes_updated: Existing rows whose content changed.
        error: ``""`` on success, or a short non-sensitive error
            string when the fetch / parse failed.
    """

    podcast_id: str
    episodes_seen: int
    episodes_created: int
    episodes_updated: int
    error: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "podcast_id": self.podcast_id,
            "episodes_seen": self.episodes_seen,
            "episodes_created": self.episodes_created,
            "episodes_updated": self.episodes_updated,
            "error": self.error,
        }


def _coerce_published(value: str | None) -> datetime | None:
    """Parse an RFC-822 / ISO-8601 published date from a feed entry."""
    if not value:
        return None
    parsed = parse_datetime(value)
    if parsed is not None:
        if django_timezone.is_naive(parsed):
            parsed = django_timezone.make_aware(parsed, UTC)
        return parsed
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        parsed = None
    if parsed is None:
        parsed_date = parse_date(value)
        if parsed_date is None:
            return None
        return django_timezone.make_aware(
            datetime.combine(parsed_date, datetime.min.time()),
            UTC,
        )
    if django_timezone.is_naive(parsed):
        parsed = django_timezone.make_aware(parsed, UTC)
    return parsed


def _coerce_duration(raw: str | None) -> int | None:
    """Parse a feed duration field (seconds, ``HH:MM:SS``, or ``MM:SS``)."""
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    parts = raw.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 3:
        h, m, s = nums
    elif len(nums) == 2:
        h, m, s = 0, nums[0], nums[1]
    else:
        return None
    return h * 3600 + m * 60 + s


def _episode_enclosures(entry: Any) -> tuple[str, str]:
    """Extract ``(audio_url, mime_type)`` from a feedparser entry."""
    enclosures = getattr(entry, "enclosures", None) or []
    for enc in enclosures:
        href = getattr(enc, "href", None) or ""
        enc_type = getattr(enc, "type", None) or ""
        if href and (enc_type.startswith("audio/") or enc_type == ""):
            return href, enc_type
    return "", ""


def fetch_feed(
    feed_url: str,
    *,
    timeout_seconds: float,
    client: httpx.Client | None = None,
) -> bytes:
    """Fetch the raw bytes of a feed.

    Raises:
        PodcastIngestionError: when the request fails or returns
            non-2xx.
    """
    if client is None:
        owned: httpx.Client = httpx.Client(
            timeout=timeout_seconds, follow_redirects=True
        )
        try:
            response = _do_get(owned, feed_url)
        finally:
            owned.close()
    else:
        response = _do_get(client, feed_url)
    if response.status_code >= 400:
        raise PodcastIngestionError(f"HTTP {response.status_code} from feed")
    return response.content


def _do_get(client: httpx.Client, feed_url: str) -> httpx.Response:
    """Issue a GET against the feed and surface errors as ingest errors."""
    try:
        return client.get(
            feed_url,
            headers={
                "User-Agent": (
                    "farm-intelligence-platform-podcasts/1.0 (+https://example.test)"
                ),
                "Accept": (
                    "application/rss+xml, application/atom+xml, "
                    "application/xml;q=0.9, */*;q=0.5"
                ),
            },
        )
    except httpx.HTTPError as exc:
        raise PodcastIngestionError(
            f"fetch failed: {type(exc).__name__}"
        ) from exc


def parse_feed_bytes(content: bytes) -> Any:
    """Parse a feed document and return the raw ``feedparser`` result.

    The returned object exposes ``feed`` (channel metadata) and
    ``entries`` (list of items). Bozo errors are tolerated; if every
    entry is missing the caller treats the feed as empty.
    """
    return feedparser.parse(content)


def _entry_audio_url(entry: Any) -> str:
    """Return the audio URL for a feed entry, or ``""`` when absent."""
    enclosures = getattr(entry, "enclosures", None) or []
    for enc in enclosures:
        href = getattr(enc, "href", None) or ""
        enc_type = getattr(enc, "type", None) or ""
        if href and (enc_type.startswith("audio/") or enc_type == ""):
            return href
    return ""


def _entry_published(entry: Any) -> datetime | None:
    """Return the published ``datetime`` of a feed entry, or ``None``."""
    raw = getattr(entry, "published", None) or getattr(entry, "updated", None)
    return _coerce_published(raw)


def _entry_image_url(entry: Any) -> str:
    """Return the image URL of a feed entry, or ``""`` when absent."""
    image = getattr(entry, "image", None)
    if isinstance(image, dict):
        href = image.get("href", "")
        return str(href or "")
    return ""


def _build_episode_fields(
    entry: Any,
    *,
    podcast: Podcast,
) -> dict[str, Any] | None:
    """Translate a feed entry into a ``PodcastEpisode`` field dict.

    Returns ``None`` when the entry is missing the required fields
    (audio URL + guid + title); such entries are skipped silently.
    """
    guid = getattr(entry, "id", None) or getattr(entry, "link", None) or ""
    if not guid:
        return None
    title = getattr(entry, "title", None) or ""
    if not title:
        return None
    audio_url, audio_type = _episode_enclosures(entry)
    if not audio_url:
        return None
    return {
        "guid": guid[:200],
        "title": title[:500],
        "description": (
            getattr(entry, "summary", None)
            or getattr(entry, "description", None)
            or ""
        ),
        "audio_url": audio_url,
        "audio_mime_type": audio_type[:100],
        "duration_seconds": _coerce_duration(
            getattr(entry, "itunes_duration", None)
        ),
        "published_at": _entry_published(entry),
        "image_url": _entry_image_url(entry)[:200],
    }


def _fields_changed(existing: PodcastEpisode, fields: dict[str, Any]) -> bool:
    for key, value in fields.items():
        if getattr(existing, key) != value:
            return True
    return False


def ingest_podcast(
    podcast: Podcast,
    *,
    timeout_seconds: float,
    client: httpx.Client | None = None,
) -> IngestionReport:
    """Fetch ``podcast.feed_url`` and upsert every episode.

    Side effects:
        - Inserts / updates ``PodcastEpisode`` rows.
        - Updates ``Podcast.last_refreshed_at``,
          ``last_refresh_status``, and ``last_refresh_error``.

    Returns:
        :class:`IngestionReport` summarising the run. The function
        never raises on a feed problem; it returns a report with
        ``error`` set instead, so a single bad feed cannot block
        the periodic task.
    """
    try:
        content = fetch_feed(
            podcast.feed_url,
            timeout_seconds=timeout_seconds,
            client=client,
        )
        parsed = parse_feed_bytes(content)
    except PodcastIngestionError as exc:
        return _mark_failure(podcast, str(exc))
    except Exception as exc:
        logger.exception(
            "podcasts_ingest_unexpected_error podcast_id=%s", podcast.id
        )
        return _mark_failure(podcast, f"unexpected: {type(exc).__name__}")

    close_old_connections()
    entries = getattr(parsed, "entries", None) or []
    created = 0
    updated = 0
    for entry in entries:
        fields = _build_episode_fields(entry, podcast=podcast)
        if fields is None:
            continue
        existing = PodcastEpisode.objects.filter(
            podcast=podcast, guid=fields["guid"]
        ).first()
        if existing is None:
            PodcastEpisode.objects.create(podcast=podcast, **fields)
            created += 1
            continue
        if _fields_changed(existing, fields):
            for key, value in fields.items():
                setattr(existing, key, value)
            existing.save()
            updated += 1

    podcast.last_refreshed_at = django_timezone.now()
    podcast.last_refresh_status = "ok"
    podcast.last_refresh_error = ""
    # Reset backoff state on success (no-op if it was already 0/None).
    if podcast.consecutive_failures or podcast.next_retry_at:
        podcast.consecutive_failures = 0
        podcast.next_retry_at = None
    podcast.save(
        update_fields=[
            "last_refreshed_at",
            "last_refresh_status",
            "last_refresh_error",
            "consecutive_failures",
            "next_retry_at",
            "updated_at",
        ]
    )
    logger.info(
        "podcasts_ingested podcast_id=%s seen=%d created=%d updated=%d",
        podcast.id,
        len(entries),
        created,
        updated,
    )
    return IngestionReport(
        podcast_id=podcast.id,
        episodes_seen=len(entries),
        episodes_created=created,
        episodes_updated=updated,
        error="",
    )


def _mark_failure(podcast: Podcast, error: str) -> IngestionReport:
    error = error[:500]
    podcast.last_refreshed_at = django_timezone.now()
    podcast.last_refresh_status = "error"
    podcast.last_refresh_error = error
    podcast.consecutive_failures = (podcast.consecutive_failures or 0) + 1
    podcast.next_retry_at = _backoff_next_attempt(podcast.consecutive_failures)
    podcast.save(
        update_fields=[
            "last_refreshed_at",
            "last_refresh_status",
            "last_refresh_error",
            "consecutive_failures",
            "next_retry_at",
            "updated_at",
        ]
    )
    logger.warning(
        "podcasts_ingest_failed podcast_id=%s error=%s "
        "consecutive_failures=%d next_retry_at=%s",
        podcast.id,
        error,
        podcast.consecutive_failures,
        podcast.next_retry_at.isoformat() if podcast.next_retry_at else "",
    )
    return IngestionReport(
        podcast_id=podcast.id,
        episodes_seen=0,
        episodes_created=0,
        episodes_updated=0,
        error=error,
    )


# Per-feed backoff schedule (consecutive_failures -> seconds to wait).
# 1m, 5m, 1h, 24h; anything past that stays at 24h.
_BACKOFF_SECONDS: tuple[int, ...] = (60, 300, 3600, 86_400)


def _backoff_next_attempt(consecutive_failures: int) -> datetime:
    """Return the next allowed refresh time for a feed that just failed.

    The schedule is intentionally aggressive on the first few
    failures (1m, 5m) so a transient upstream blip self-heals
    quickly, then ramps to 1h / 24h so a permanently-broken feed
    stops being hammered.
    """
    idx = max(0, min(consecutive_failures - 1, len(_BACKOFF_SECONDS) - 1))
    delay_s = _BACKOFF_SECONDS[idx]
    return django_timezone.now() + timedelta(seconds=delay_s)


def clear_backoff(podcast: Podcast) -> None:
    """Reset backoff state on a successful refresh.

    Idempotent: safe to call multiple times. Called from
    :func:`ingest_podcast` after a successful run, and exposed so
    the per-feed Celery task can apply the same reset on the
    re-fetch path.
    """
    if podcast.consecutive_failures == 0 and podcast.next_retry_at is None:
        return
    podcast.consecutive_failures = 0
    podcast.next_retry_at = None
    podcast.save(
        update_fields=[
            "consecutive_failures",
            "next_retry_at",
            "updated_at",
        ]
    )


def ingest_all_active_podcasts(
    *,
    timeout_seconds: float,
) -> list[IngestionReport]:
    """Ingest every active podcast. Suitable for Celery beat."""
    reports: list[IngestionReport] = []
    for podcast in Podcast.objects.filter(is_active=True).order_by("id"):
        reports.append(
            ingest_podcast(podcast, timeout_seconds=timeout_seconds)
        )
    return reports


def list_episodes_for_podcast(
    podcast: Podcast,
    *,
    limit: int | None = None,
) -> list[PodcastEpisode]:
    """Return ``podcast``'s episodes, newest first."""
    qs = PodcastEpisode.objects.filter(podcast=podcast)
    if limit is not None:
        qs = qs[:limit]
    return list(qs)


def get_refresh_timeout_seconds() -> float:
    """Read the per-feed timeout from settings with a sane default."""
    return float(getattr(settings, "PODCASTS_REFRESH_TIMEOUT_SECONDS", 15.0))
