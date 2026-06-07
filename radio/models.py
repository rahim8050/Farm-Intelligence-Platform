from __future__ import annotations

from django.conf import settings
from django.db import models


class ProviderType(models.TextChoices):
    BROADCASTER = "broadcaster", "Direct Broadcaster"
    AGGREGATOR = "aggregator", "Station Aggregator"
    API_BASED = "api_based", "API-driven"


class Provider(models.Model):
    """Radio streaming provider."""

    slug = models.SlugField(max_length=50, primary_key=True)
    name = models.CharField(max_length=200)
    provider_type = models.CharField(
        max_length=20,
        choices=ProviderType.choices,
        default=ProviderType.BROADCASTER,
    )
    api_endpoint = models.URLField(blank=True)
    api_key = models.CharField(max_length=500, blank=True)
    website_url = models.URLField(blank=True)
    logo_url = models.URLField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "radio_provider"
        verbose_name = "Provider"
        verbose_name_plural = "Providers"

    def __str__(self) -> str:
        return self.name


class Station(models.Model):
    """Radio station with streaming metadata."""

    id = models.CharField(max_length=50, primary_key=True)
    name = models.CharField(max_length=200)
    provider = models.ForeignKey(
        Provider,
        on_delete=models.PROTECT,
        related_name="stations",
    )
    genre = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=100)
    language = models.CharField(max_length=100)
    stream_url = models.URLField()
    format = models.CharField(max_length=20, default="MP3")
    bitrate = models.IntegerField(default=128)
    logo_url = models.URLField(blank=True)
    website_url = models.URLField(blank=True)
    is_active = models.BooleanField(default=True)
    is_available = models.BooleanField(
        null=True,
        blank=True,
        help_text=(
            "Set by the periodic health check. None = never checked, "
            "True = reachable, False = unreachable."
        ),
    )
    last_health_check_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp of the most recent health check.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "radio_station"
        verbose_name = "Station"
        verbose_name_plural = "Stations"
        ordering = ["name"]
        indexes = [
            models.Index(fields=["is_active"]),
            models.Index(fields=["provider"]),
            models.Index(fields=["genre"]),
            models.Index(fields=["is_available"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.provider.name})"


class StationHealthCheck(models.Model):
    """A single health-check probe of a station's stream URL.

    One row per probe. The most recent row per station drives
    `Station.is_available` and the `radio_last_health_check_at`
    timestamp. Older rows are kept for audit and trending.
    """

    station = models.ForeignKey(
        Station,
        on_delete=models.CASCADE,
        related_name="health_checks",
    )
    checked_at = models.DateTimeField(auto_now_add=True, db_index=True)
    is_reachable = models.BooleanField()
    response_time_ms = models.IntegerField(null=True, blank=True)
    status_code = models.IntegerField(null=True, blank=True)
    error_message = models.TextField(blank=True)

    class Meta:
        db_table = "radio_station_health_check"
        verbose_name = "Station health check"
        verbose_name_plural = "Station health checks"
        ordering = ["-checked_at"]
        indexes = [
            models.Index(fields=["station", "-checked_at"]),
        ]

    def __str__(self) -> str:
        return (
            f"{self.station_id} "
            f"{'up' if self.is_reachable else 'down'} "
            f"@ {self.checked_at.isoformat()}"
        )


class Favorite(models.Model):
    """A user's favorite radio station.

    One row per (user, station). The pair is unique, so a user can
    favorite a given station at most once. The model is intentionally
    lean: see ``radio.services`` for the business rules that surround
    it (idempotent add/remove, listing, etc.).
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="radio_favorites",
    )
    station = models.ForeignKey(
        Station,
        on_delete=models.CASCADE,
        related_name="favorited_by",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "radio_favorite"
        verbose_name = "Favorite"
        verbose_name_plural = "Favorites"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "station"],
                name="radio_favorite_user_station_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} -> {self.station_id}"


class ListeningHistory(models.Model):
    """A row recording that a user fetched a station's stream URL.

    Each successful ``GET /api/v1/radio/stations/<id>/stream/`` from an
    authenticated client creates one row. Rows are kept for trending
    and a "recently played" surface; ``ended_at`` is reserved for a
    future client-driven stop event.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="radio_listening_history",
    )
    station = models.ForeignKey(
        Station,
        on_delete=models.CASCADE,
        related_name="listening_history",
    )
    started_at = models.DateTimeField(auto_now_add=True, db_index=True)
    ended_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "Reserved for a future client-driven stop event. "
            "Currently always NULL."
        ),
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=200, blank=True)

    class Meta:
        db_table = "radio_listening_history"
        verbose_name = "Listening history"
        verbose_name_plural = "Listening history"
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["user", "-started_at"]),
        ]

    def __str__(self) -> str:
        ts = self.started_at.isoformat()
        return f"{self.user_id} -> {self.station_id} @ {ts}"


class EmergencyPriority(models.TextChoices):
    """Severity levels for an :class:`EmergencyBroadcast`."""

    LOW = "low", "Low"
    MEDIUM = "medium", "Medium"
    HIGH = "high", "High"
    CRITICAL = "critical", "Critical"


class EmergencyBroadcast(models.Model):
    """An emergency broadcast message surfaced by the radio service.

    Used for weather alerts, farm emergency notifications, and other
    critical system messages. A broadcast is "active" when
    ``is_active`` is true and the current time falls inside the
    ``[starts_at, ends_at]`` window. Read endpoints are public; create
    / update / delete are restricted to admins.

    The design follows ``docs/architecture/radio/08_future_expansion.md``
    (P5 — Emergency broadcasts).
    """

    id = models.BigAutoField(primary_key=True)
    title = models.CharField(max_length=200)
    message = models.TextField()
    priority = models.CharField(
        max_length=20,
        choices=EmergencyPriority.choices,
        default=EmergencyPriority.MEDIUM,
    )
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="emergency_broadcasts_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "radio_emergency_broadcast"
        verbose_name = "Emergency broadcast"
        verbose_name_plural = "Emergency broadcasts"
        ordering = ["-priority", "-starts_at"]
        indexes = [
            models.Index(fields=["is_active", "starts_at", "ends_at"]),
            models.Index(fields=["priority", "-starts_at"]),
        ]

    def __str__(self) -> str:
        return f"[{self.priority}] {self.title}"
