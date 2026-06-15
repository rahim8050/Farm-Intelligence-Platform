"""Radio business logic.

This module encapsulates health-check and station-availability logic so
that views and Celery tasks can stay thin. Per
``docs/architecture/radio/04_app_structure.md`` the service layer is
the intended home for business rules.

Auth: caller-controlled. The service layer does not enforce auth.
Response: this layer returns plain Python data; HTTP shape is the
view's responsibility.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, AnonymousUser
from django.core.cache import cache
from django.db import close_old_connections, transaction
from django.db.models import Count
from django.http import HttpRequest
from django.utils import timezone

from radio.models import (
    EmergencyBroadcast,
    Favorite,
    ListeningHistory,
    NowPlaying,
    Station,
    StationAnalytics,
    StationHealthCheck,
)

logger = logging.getLogger("radio")

STATION_LIST_CACHE_KEY = "radio:stations:all"
STATION_LIST_CACHE_TTL_SECONDS = 60

# Default fallback-station map. If a station is unavailable, the
# ``/api/v1/radio/stations/<id>/stream/`` endpoint includes the
# fallback's stream URL in the 503 payload so the client can pivot
# without a second round-trip. Operators can override this map via
# the ``RADIO_FALLBACK_STATION_MAP`` Django setting (a flat dict of
# ``{primary_id: fallback_id}``).
DEFAULT_FALLBACK_STATION_MAP: dict[str, str] = {
    "bbc_1xtra": "bbc_radio1",
    "bbc_radio1": "bbc_radio2",
    "bbc_radio2": "bbc_radio1",
}

# Cap on how much text we are willing to scan when looking for the
# ICY ``StreamTitle=`` line. Real metadata blocks are < 16 KiB; this
# cap is a defensive bound for misbehaving servers.
NOW_PLAYING_MAX_METADATA_BYTES = 16 * 1024
NOW_PLAYING_ENRICH_TIMEOUT_SECONDS = 3.0
NOW_PLAYING_HTTP_TIMEOUT_SECONDS = 5.0


class StationUnavailableError(Exception):
    """Raised when a station's stream URL is not reachable."""

    def __init__(self, station_id: str, reason: str) -> None:
        self.station_id = station_id
        self.reason = reason
        super().__init__(f"Station {station_id} unavailable: {reason}")


@dataclass(frozen=True)
class HealthProbeResult:
    """Outcome of a single station health probe.

    Attributes:
        station_id: Station primary key.
        is_reachable: True if the HTTP probe succeeded with 2xx/3xx.
        status_code: HTTP status code, or None if the request failed.
        response_time_ms: Wall-clock latency of the probe in ms, or None.
        error_message: Short non-sensitive description of the failure,
            or empty string on success.
    """

    station_id: str
    is_reachable: bool
    status_code: int | None
    response_time_ms: int | None
    error_message: str

    def to_dict(self) -> dict[str, object]:
        return {
            "station_id": self.station_id,
            "is_reachable": self.is_reachable,
            "status_code": self.status_code,
            "response_time_ms": self.response_time_ms,
            "error_message": self.error_message,
        }


def probe_station(
    station: Station,
    *,
    timeout_seconds: float,
    client: httpx.Client | None = None,
) -> HealthProbeResult:
    """Probe a single station's stream URL via HEAD.

    The probe uses HEAD to minimise bandwidth, but falls back to a
    ranged GET if the server returns 405/501. This matches the design
    note in ``docs/architecture/radio/09_operational.md`` and tolerates
    providers that do not support HEAD.

    Args:
        station: The station to probe.
        timeout_seconds: Request timeout in seconds.
        client: Optional pre-configured ``httpx.Client`` for testing or
            connection pooling. A new client is created per call when
            ``None``.

    Returns:
        A ``HealthProbeResult`` describing the outcome. The caller is
        responsible for persisting the result and updating aggregate
        state.
    """
    own_client = client is None
    if own_client:
        client = httpx.Client(timeout=timeout_seconds, follow_redirects=True)

    start = time.monotonic()
    try:
        try:
            response = client.head(station.stream_url)
            if response.status_code in (405, 501):
                response = client.get(
                    station.stream_url,
                    headers={"Range": "bytes=0-0"},
                )
        except httpx.HTTPError as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            return HealthProbeResult(
                station_id=station.id,
                is_reachable=False,
                status_code=None,
                response_time_ms=elapsed_ms,
                error_message=_safe_error(exc),
            )
    finally:
        if own_client:
            client.close()

    elapsed_ms = int((time.monotonic() - start) * 1000)
    is_reachable = response.status_code < 400
    return HealthProbeResult(
        station_id=station.id,
        is_reachable=is_reachable,
        status_code=response.status_code,
        response_time_ms=elapsed_ms,
        error_message=("" if is_reachable else f"HTTP {response.status_code}"),
    )


def _safe_error(exc: Exception) -> str:
    """Return a non-sensitive, non-PII error string for persistence."""
    name = type(exc).__name__
    message = str(exc)
    if not message:
        return name
    if len(message) > 200:
        message = message[:200]
    return f"{name}: {message}"


def record_probe_result(
    station: Station,
    result: HealthProbeResult,
) -> StationHealthCheck:
    """Persist a probe result and update the station's aggregate state.

    Side effects:
        - Inserts a new ``StationHealthCheck`` row.
        - Updates ``Station.is_available`` and
          ``Station.last_health_check_at`` to reflect the probe.

    Returns:
        The newly created ``StationHealthCheck`` row.
    """
    close_old_connections()
    check = StationHealthCheck.objects.create(
        station=station,
        is_reachable=result.is_reachable,
        response_time_ms=result.response_time_ms,
        status_code=result.status_code,
        error_message=result.error_message,
    )
    station.is_available = result.is_reachable
    station.last_health_check_at = check.checked_at
    station.save(
        update_fields=["is_available", "last_health_check_at", "updated_at"]
    )
    return check


def probe_and_record(
    station: Station,
    *,
    timeout_seconds: float,
    client: httpx.Client | None = None,
) -> HealthProbeResult:
    """Probe a station and persist the result.

    Convenience wrapper that combines :func:`probe_station` and
    :func:`record_probe_result`.
    """
    result = probe_station(
        station, timeout_seconds=timeout_seconds, client=client
    )
    record_probe_result(station, result)
    return result


def probe_all_active_stations(
    *,
    timeout_seconds: float,
    client: httpx.Client | None = None,
) -> list[HealthProbeResult]:
    """Probe every active station and persist the results.

    Iterates ``Station.objects.filter(is_active=True)`` in a stable
    order and calls :func:`probe_and_record` for each. Suitable for
    periodic Celery invocation.

    Returns:
        List of probe results, in the same order as the input query.
    """
    stations = list(Station.objects.filter(is_active=True).order_by("id"))
    own_client = client is None
    if own_client:
        client = httpx.Client(timeout=timeout_seconds, follow_redirects=True)
    try:
        return [
            probe_and_record(
                station, timeout_seconds=timeout_seconds, client=client
            )
            for station in stations
        ]
    finally:
        if own_client:
            client.close()


def summarize_health() -> dict[str, object]:
    """Build a small health summary for the ``/api/v1/radio/health/`` view.

    The aggregate ``Station.is_available`` field is the source of truth
    for current availability. The summary distinguishes three states:

    - ``available``: ``is_available=True`` (most recent probe succeeded)
    - ``unavailable``: ``is_available=False`` (most recent probe failed)
    - ``unchecked``: ``is_available IS NULL`` (never been probed)

    Status flag:
        - ``healthy``: every active station is available.
        - ``unhealthy``: at least one station was probed, and none are
          available.
        - ``degraded``: anything in between, or no stations configured.
    """
    now: datetime = timezone.now()
    stations_qs = Station.objects.filter(is_active=True)
    stations_total = stations_qs.count()
    reachable = stations_qs.filter(is_available=True).count()
    unreachable = stations_qs.filter(is_available=False).count()
    unchecked = max(stations_total - reachable - unreachable, 0)

    if stations_total == 0:
        status = "degraded"
    elif reachable == stations_total and unreachable == 0:
        status = "healthy"
    elif reachable == 0 and unreachable > 0:
        status = "unhealthy"
    else:
        status = "degraded"

    return {
        "status": status,
        "timestamp": now,
        "stations_total": stations_total,
        "stations_available": reachable,
        "stations_unavailable": unreachable,
        "stations_unchecked": unchecked,
    }


# ---------------------------------------------------------------------------
# Favorites & listening history (Phase 3)
# ---------------------------------------------------------------------------


def list_active_stations_cached() -> list[Station]:
    """Return the list of active stations, cached for
    ``STATION_LIST_CACHE_TTL_SECONDS`` seconds.

    The station list is small and changes infrequently, but the
    public ``/api/v1/radio/stations/`` endpoint is hit on every
    page load by clients. Caching it cuts DB round-trips without
    staleness concerns: the cache is invalidated on station
    create/update via the ``station_saved`` signal, and it expires
    after ``STATION_LIST_CACHE_TTL_SECONDS`` regardless.
    """
    cached = cache.get(STATION_LIST_CACHE_KEY)
    if cached is not None:
        return list(cached)
    stations = list(
        Station.objects.filter(is_active=True)
        .select_related("provider")
        .order_by("name")
    )
    cache.set(STATION_LIST_CACHE_KEY, stations, STATION_LIST_CACHE_TTL_SECONDS)
    return stations


def invalidate_station_list_cache() -> None:
    """Drop the cached station list. Called from station signals."""
    cache.delete(STATION_LIST_CACHE_KEY)


def get_favorite(
    user: AbstractBaseUser | AnonymousUser | Any,
    station_id: str,
) -> Favorite | None:
    """Return the user's favorite for ``station_id`` if it exists."""
    return Favorite.objects.filter(user=user, station_id=station_id).first()


def add_favorite(
    user: AbstractBaseUser | AnonymousUser | Any,
    station: Station,
) -> tuple[Favorite, bool]:
    """Idempotently add ``station`` to ``user``'s favorites.

    Returns:
        ``(favorite, created)`` where ``created`` is ``True`` only when
        a new row was inserted. Calling ``add_favorite`` twice with the
        same (user, station) is safe and returns the original row.
    """
    favorite, created = Favorite.objects.get_or_create(
        user=user, station=station
    )
    if created:
        logger.info(
            "radio_favorite_added user_id=%s station_id=%s",
            getattr(user, "id", None),
            station.id,
            extra={
                "event": "radio_favorite_added",
                "user_id": getattr(user, "id", None),
                "station_id": station.id,
            },
        )
    return favorite, created


def remove_favorite(
    user: AbstractBaseUser | AnonymousUser | Any,
    station_id: str,
) -> bool:
    """Idempotently remove the favorite matching (user, station_id).

    Returns:
        ``True`` when a row was deleted; ``False`` when no row existed.
    """
    deleted, _ = Favorite.objects.filter(
        user=user, station_id=station_id
    ).delete()
    if deleted:
        logger.info(
            "radio_favorite_removed user_id=%s station_id=%s",
            getattr(user, "id", None),
            station_id,
            extra={
                "event": "radio_favorite_removed",
                "user_id": getattr(user, "id", None),
                "station_id": station_id,
            },
        )
    return bool(deleted)


def list_favorites_for_user(
    user: AbstractBaseUser | AnonymousUser | Any,
    *,
    limit: int | None = None,
) -> list[Favorite]:
    """Return the user's favorites, newest first.

    The optional ``limit`` caps the result set; the default of ``None``
    returns the full list.
    """
    qs = Favorite.objects.filter(user=user).select_related(
        "station", "station__provider"
    )
    if limit is not None:
        qs = qs[:limit]
    return list(qs)


def record_listening_session(
    user: AbstractBaseUser | AnonymousUser | Any,
    station: Station,
    request: HttpRequest | None = None,
) -> ListeningHistory | None:
    """Record a listening-history row for an authenticated user.

    Returns:
        The created :class:`ListeningHistory` row, or ``None`` when the
        caller is anonymous (history is per-user; we do not record
        anonymous traffic).

    The row is best-effort: a failure to insert is logged and swallowed
    so it can never block a stream URL from being served.
    """
    user_id = getattr(user, "id", None)
    if user_id is None:
        return None
    ip_address: str | None = None
    user_agent: str = ""
    if request is not None:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            ip_address = forwarded.split(",")[0].strip() or None
        if ip_address is None:
            ip_address = request.META.get("REMOTE_ADDR") or None
        user_agent = (request.META.get("HTTP_USER_AGENT") or "")[:200]
    try:
        return ListeningHistory.objects.create(
            user=user,
            station=station,
            ip_address=ip_address,
            user_agent=user_agent,
        )
    except Exception:
        logger.exception(
            "radio_listening_history_record_failed user_id=%s station_id=%s",
            user_id,
            station.id,
            extra={
                "event": "radio_listening_history_record_failed",
                "user_id": user_id,
                "station_id": station.id,
            },
        )
        return None


def stop_listening_session(
    user: AbstractBaseUser | AnonymousUser | Any,
    session_id: int,
) -> ListeningHistory | None:
    """Set ``ended_at`` on a listening-history row (client stop).

    The session must belong to the given user; otherwise ``None`` is
    returned and nothing is modified (the caller should return 404).

    Idempotent: calling again on an already-stopped session is a no-op
    (returns the row as-is).
    """
    try:
        session = ListeningHistory.objects.get(id=session_id, user=user)
    except ListeningHistory.DoesNotExist:
        return None
    if session.ended_at is None:
        session.ended_at = timezone.now()
        session.save(update_fields=["ended_at"])
    return session


def list_history_for_user(
    user: AbstractBaseUser | AnonymousUser | Any,
    *,
    limit: int | None = None,
) -> list[ListeningHistory]:
    """Return the user's listening history, newest first."""
    qs = ListeningHistory.objects.filter(user=user).select_related(
        "station", "station__provider"
    )
    if limit is not None:
        qs = qs[:limit]
    return list(qs)


# ---------------------------------------------------------------------------
# Emergency broadcasts (Phase 5 / P5)
# ---------------------------------------------------------------------------


def get_current_emergency(
    now: datetime | None = None,
) -> EmergencyBroadcast | None:
    """Return the most severe active :class:`EmergencyBroadcast`.

    "Active" means ``is_active=True`` and the current time falls inside
    the ``[starts_at, ends_at]`` window. The function returns the
    highest-priority broadcast (critical > high > medium > low), then
    the most recently started.
    """
    current = now or timezone.now()
    priority_rank = {
        "critical": 0,
        "high": 1,
        "medium": 2,
        "low": 3,
    }
    qs = EmergencyBroadcast.objects.filter(
        is_active=True,
        starts_at__lte=current,
        ends_at__gte=current,
    )
    items = list(qs)
    if not items:
        return None
    items.sort(
        key=lambda b: (
            priority_rank.get(b.priority, 99),
            -b.starts_at.timestamp(),
        )
    )
    return items[0]


def list_emergency_history(
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[EmergencyBroadcast]:
    """Return a page of :class:`EmergencyBroadcast` rows, newest first.

    Args:
        limit: Maximum number of rows to return (1..200, default 50).
        offset: Number of rows to skip from the top of the list.
    """
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    return list(
        EmergencyBroadcast.objects.order_by("-starts_at", "-id")[
            offset : offset + limit
        ]
    )


def create_emergency_broadcast(
    *,
    title: str,
    message: str,
    priority: str,
    starts_at: datetime,
    ends_at: datetime,
    is_active: bool = True,
    created_by: AbstractBaseUser | AnonymousUser | Any | None = None,
) -> EmergencyBroadcast:
    """Create a new :class:`EmergencyBroadcast` row.

    Side effects: inserts a new row and emits a structured log line.
    """
    broadcast = EmergencyBroadcast.objects.create(
        title=title,
        message=message,
        priority=priority,
        starts_at=starts_at,
        ends_at=ends_at,
        is_active=is_active,
        created_by=created_by
        if getattr(created_by, "is_authenticated", False)
        else None,
    )
    logger.info(
        "radio_emergency_broadcast_created id=%s priority=%s "
        "starts_at=%s ends_at=%s",
        broadcast.id,
        broadcast.priority,
        broadcast.starts_at.isoformat(),
        broadcast.ends_at.isoformat(),
        extra={
            "event": "radio_emergency_broadcast_created",
            "broadcast_id": broadcast.id,
            "priority": broadcast.priority,
            "starts_at": broadcast.starts_at.isoformat(),
            "ends_at": broadcast.ends_at.isoformat(),
        },
    )
    return broadcast


def update_emergency_broadcast(
    broadcast: EmergencyBroadcast,
    *,
    fields: dict[str, Any],
) -> EmergencyBroadcast:
    """Apply ``fields`` to ``broadcast`` and persist the change.

    Only attributes that exist on the model are applied. Side effects:
    one row update; no cascade.
    """
    editable = {
        "title",
        "message",
        "priority",
        "starts_at",
        "ends_at",
        "is_active",
    }
    for key, value in fields.items():
        if key in editable:
            setattr(broadcast, key, value)
    broadcast.save()
    return broadcast


def delete_emergency_broadcast(broadcast: EmergencyBroadcast) -> None:
    """Delete an :class:`EmergencyBroadcast` row. Idempotent."""
    EmergencyBroadcast.objects.filter(pk=broadcast.pk).delete()


# ---------------------------------------------------------------------------
# Fallback stations (Phase 7)
# ---------------------------------------------------------------------------


def get_fallback_station_map() -> dict[str, str]:
    """Return the effective fallback-station map.

    The operator-provided ``RADIO_FALLBACK_STATION_MAP`` setting
    wins over :data:`DEFAULT_FALLBACK_STATION_MAP`. Operators can
    set the setting to an empty dict to disable every fallback
    (useful for tests / staging). If the setting is not configured
    at all (``None``) the defaults are used.
    """
    raw = getattr(settings, "RADIO_FALLBACK_STATION_MAP", None)
    if raw is None:
        return dict(DEFAULT_FALLBACK_STATION_MAP)
    if not isinstance(raw, dict):
        return dict(DEFAULT_FALLBACK_STATION_MAP)
    return {str(k): str(v) for k, v in raw.items()}


def get_fallback_station(primary_id: str) -> Station | None:
    """Return the fallback :class:`Station` for ``primary_id``, if any.

    The lookup uses :func:`get_fallback_station_map`; the fallback
    station must be ``is_active=True`` and must have a populated
    stream URL. ``None`` means "no fallback configured / fallback
    inactive".
    """
    mapping = get_fallback_station_map()
    fallback_id = mapping.get(primary_id)
    if not fallback_id or fallback_id == primary_id:
        return None
    return (
        Station.objects.filter(id=fallback_id, is_active=True)
        .only("id", "name", "stream_url", "format", "bitrate", "is_available")
        .first()
    )


# ---------------------------------------------------------------------------
# Station analytics rollup (Phase 7)
# ---------------------------------------------------------------------------


def rollup_station_analytics(
    *,
    lookback_days: int = 1,
    target_date: date | None = None,
) -> dict[str, int]:
    """Roll :class:`ListeningHistory` rows into
    :class:`StationAnalytics` rows.

    Args:
        lookback_days: How many days back from ``target_date`` (or
            today) to scan. The default of 1 covers the
            just-finished UTC day; the periodic task uses 2 so the
            previous day is re-aggregated after midnight.
        target_date: Reference date for the rollup window. ``None``
            means ``timezone.now().date()``.

    Returns:
        Dict with ``stations_processed``, ``days_processed``,
        ``rows_written``.
    """
    target = target_date or timezone.now().date()
    rows_written = 0
    stations_processed = 0
    for offset in range(lookback_days):
        day = target - timedelta(days=offset)
        day_start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
        day_end = day_start + timedelta(days=1)
        per_station = (
            ListeningHistory.objects.filter(
                started_at__gte=day_start, started_at__lt=day_end
            )
            .values("station_id")
            .annotate(
                total_listens=Count("id"),
                unique_users=Count("user_id", distinct=True),
            )
        )
        for row in per_station:
            station_id = row["station_id"]
            with transaction.atomic():
                obj, _ = StationAnalytics.objects.update_or_create(
                    station_id=station_id,
                    date=day,
                    defaults={
                        "total_listens": row["total_listens"],
                        "unique_users": row["unique_users"],
                    },
                )
                rows_written += 1
                stations_processed += 1
    logger.info(
        "radio_analytics_rollup_completed days=%d stations=%d rows=%d",
        lookback_days,
        stations_processed,
        rows_written,
        extra={
            "event": "radio_analytics_rollup_completed",
            "lookback_days": lookback_days,
            "stations_processed": stations_processed,
            "rows_written": rows_written,
        },
    )
    return {
        "stations_processed": stations_processed,
        "days_processed": lookback_days,
        "rows_written": rows_written,
    }


def get_station_analytics(
    station_id: str,
    *,
    days: int = 7,
) -> list[StationAnalytics]:
    """Return up to ``days`` of :class:`StationAnalytics` rows for
    ``station_id``, newest first."""
    days = max(1, min(int(days), 90))
    return list(
        StationAnalytics.objects.filter(station_id=station_id).order_by(
            "-date"
        )[:days]
    )


# ---------------------------------------------------------------------------
# Now-playing ingestion (Phase 7)
# ---------------------------------------------------------------------------


def _parse_icy_stream_title(metadata_text: str) -> str:
    """Extract the ``StreamTitle=...;`` value from an ICY metadata blob.

    The ICY protocol stores stream metadata as
    ``StreamTitle='Artist - Title';StreamUrl='...';`` inside the
    body of a periodic metadata block. We pull the first
    ``StreamTitle=`` value and decode it as UTF-8 (with a Latin-1
    fallback for older servers).
    """
    text = metadata_text
    idx = text.find("StreamTitle=")
    if idx < 0:
        return ""
    after = text[idx + len("StreamTitle=") :]
    quote = after.find("'")
    if quote < 0:
        return after.split(";", 1)[0].strip()
    end = after.find("'", quote + 1)
    if end < 0:
        # Unterminated string: take everything after the opening
        # quote up to the next semicolon (or the end of the blob).
        semi = after.find(";", quote + 1)
        raw = after[quote + 1 : semi if semi > quote else len(after)]
    else:
        raw = after[quote + 1 : end]
    try:
        return raw.encode("latin-1", errors="replace").decode(
            "utf-8", errors="replace"
        )
    except Exception:  # noqa: BLE001
        return raw


def fetch_icy_metadata(url: str) -> dict[str, str]:
    """Fetch an ICY / SHOUTcast stream URL and return the parsed
    ``StreamTitle`` plus the separated artist / title fields.

    Best-effort: a non-ICY response, a timeout, or a missing
    ``StreamTitle`` line all return ``{}``; the caller decides
    whether to upsert the row.
    """
    try:
        with httpx.Client(
            timeout=NOW_PLAYING_HTTP_TIMEOUT_SECONDS, follow_redirects=True
        ) as client:
            response = client.get(
                url,
                headers={
                    "Icy-MetaData": "1",
                    "User-Agent": "farm-intelligence-platform/1.0",
                },
            )
    except httpx.HTTPError as exc:
        logger.info(
            "radio_icy_metadata_fetch_failed url=%s err=%s",
            url,
            exc.__class__.__name__,
        )
        return {}

    if response.status_code >= 400:
        return {}

    raw = response.content[:NOW_PLAYING_MAX_METADATA_BYTES]
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return {}

    stream_title = _parse_icy_stream_title(text)
    if not stream_title:
        return {}

    artist = ""
    title = stream_title
    if " - " in stream_title:
        artist, title = stream_title.split(" - ", 1)
    return {
        "track_title": title.strip(),
        "artist": artist.strip(),
    }


def _enrich_with_album_artwork(artist: str, title: str) -> dict[str, str]:
    """Look up album and artwork URL for a given artist + title.

    Uses the Deezer public search API (no key needed). Best-effort:
    any network error, timeout, or non-200 response silently returns
    ``{}`` so it can never break the now-playing refresh loop.
    """
    if not artist or not title:
        return {}
    query = f'artist:"{artist}" track:"{title}"'
    url = "https://api.deezer.com/search"
    try:
        with httpx.Client(
            timeout=NOW_PLAYING_ENRICH_TIMEOUT_SECONDS
        ) as client:
            response = client.get(
                url,
                params={"q": query, "limit": 1, "output": "json"},
                headers={"User-Agent": "farm-intelligence-platform/1.0"},
            )
    except httpx.HTTPError:
        return {}
    if response.status_code != 200:
        return {}
    try:
        data = response.json()
    except Exception:  # noqa: BLE001
        return {}
    items = data.get("data", [])
    if not items:
        return {}
    album = items[0].get("album", {})
    if not album:
        return {}
    result: dict[str, str] = {}
    raw_album = album.get("title", "")
    if raw_album:
        result["album"] = raw_album[:500]
    raw_artwork = album.get("cover_medium", "") or album.get("cover_big", "")
    if raw_artwork:
        result["artwork_url"] = raw_artwork
    return result


def refresh_now_playing(station_id: str | None = None) -> dict[str, int]:
    """Refresh :class:`NowPlaying` rows from each station's
    configured ``metadata_url``.

    Args:
        station_id: Optional single station to refresh. ``None``
            means "all stations with a metadata_url".

    Returns:
        Dict with ``attempted`` / ``updated`` / ``skipped`` counts.
    """
    qs = Station.objects.filter(is_active=True).exclude(metadata_url="")
    if station_id is not None:
        qs = qs.filter(id=station_id)
    attempted = 0
    updated = 0
    skipped = 0
    for station in qs:
        attempted += 1
        if not station.metadata_url:
            skipped += 1
            continue
        parsed = fetch_icy_metadata(station.metadata_url)
        if not parsed:
            skipped += 1
            continue
        enriched = _enrich_with_album_artwork(
            parsed.get("artist", ""), parsed.get("track_title", "")
        )
        defaults: dict[str, object] = {
            "track_title": parsed.get("track_title", "")[:500],
            "artist": parsed.get("artist", "")[:500],
        }
        if enriched.get("album"):
            defaults["album"] = enriched["album"]
        if enriched.get("artwork_url"):
            defaults["artwork_url"] = enriched["artwork_url"]
        with transaction.atomic():
            NowPlaying.objects.update_or_create(
                station=station,
                defaults=defaults,
            )
        updated += 1
    logger.info(
        "radio_now_playing_refresh_completed attempted=%d "
        "updated=%d skipped=%d",
        attempted,
        updated,
        skipped,
        extra={
            "event": "radio_now_playing_refresh_completed",
            "attempted": attempted,
            "updated": updated,
            "skipped": skipped,
        },
    )
    return {"attempted": attempted, "updated": updated, "skipped": skipped}


def get_now_playing(station_id: str) -> NowPlaying | None:
    """Return the cached :class:`NowPlaying` row for ``station_id``,
    or ``None`` if the station has none (no metadata URL or no
    successful poll yet)."""
    return NowPlaying.objects.filter(station_id=station_id).first()
