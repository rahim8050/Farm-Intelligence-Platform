from __future__ import annotations

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
