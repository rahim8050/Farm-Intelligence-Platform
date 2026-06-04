"""Podcast and podcast-episode models.

This app stores the catalogue of podcast shows we mirror from public
RSS/Atom feeds, plus the per-episode metadata that drives the public
``/api/v1/podcasts/`` endpoints.

The feed itself is **not** stored; we re-fetch the upstream XML on a
periodic cadence and refresh the local rows. See
``podcasts.services`` and ``podcasts.tasks``.
"""

from __future__ import annotations

from django.db import models


class Podcast(models.Model):
    """A podcast show that we mirror from an upstream RSS/Atom feed.

    ``id`` is a short, URL-safe identifier (e.g. ``"bbc_global_news"``)
    used in API paths. ``feed_url`` is the upstream feed that
    ``podcasts.services.ingest_podcast`` re-fetches on a schedule.
    """

    id = models.CharField(max_length=50, primary_key=True)
    title = models.CharField(max_length=500)
    description = models.TextField(blank=True)
    author = models.CharField(max_length=200, blank=True)
    feed_url = models.URLField()
    image_url = models.URLField(blank=True)
    language = models.CharField(max_length=20, blank=True)
    is_active = models.BooleanField(default=True)
    last_refreshed_at = models.DateTimeField(null=True, blank=True)
    last_refresh_status = models.CharField(
        max_length=20,
        blank=True,
        help_text=(
            "Outcome of the most recent ingestion: "
            '"ok", "error", or "" (never refreshed).'
        ),
    )
    last_refresh_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "podcasts_podcast"
        verbose_name = "Podcast"
        verbose_name_plural = "Podcasts"
        ordering = ["title"]

    def __str__(self) -> str:
        return self.title


class PodcastEpisode(models.Model):
    """A single episode of a :class:`Podcast`.

    The pair ``(podcast, guid)`` is unique; re-ingesting the same
    episode updates the row in place rather than creating a duplicate.
    """

    podcast = models.ForeignKey(
        Podcast,
        on_delete=models.CASCADE,
        related_name="episodes",
    )
    guid = models.CharField(max_length=200)
    title = models.CharField(max_length=500)
    description = models.TextField(blank=True)
    audio_url = models.URLField()
    audio_mime_type = models.CharField(max_length=100, blank=True)
    duration_seconds = models.IntegerField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    image_url = models.URLField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "podcasts_episode"
        verbose_name = "Podcast episode"
        verbose_name_plural = "Podcast episodes"
        ordering = ["-published_at", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["podcast", "guid"],
                name="podcasts_episode_podcast_guid_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["podcast", "-published_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.podcast_id}: {self.title}"
