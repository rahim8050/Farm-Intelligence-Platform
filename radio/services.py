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
from datetime import datetime
from typing import Any

import httpx
from django.contrib.auth.models import AbstractBaseUser, AnonymousUser
from django.core.cache import cache
from django.db import close_old_connections
from django.http import HttpRequest
from django.utils import timezone

from radio.models import (
    EmergencyBroadcast,
    Favorite,
    ListeningHistory,
    Station,
    StationHealthCheck,
)

logger = logging.getLogger("radio")

STATION_LIST_CACHE_KEY = "radio:stations:all"
STATION_LIST_CACHE_TTL_SECONDS = 60


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
