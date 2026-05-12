from __future__ import annotations

from django.db import models


class Provider(models.Model):
    """Radio streaming provider."""

    slug = models.SlugField(max_length=50, primary_key=True)
    name = models.CharField(max_length=200)
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
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.provider.name})"
