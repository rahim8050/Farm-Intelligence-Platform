"""Seed a small catalogue of well-known public RSS feeds.

The feeds listed here are widely re-distributed public broadcasts;
they exist solely as a development convenience so the /podcasts/
endpoints have something to return out of the box. The command is
idempotent: re-running it updates titles / authors but does not
duplicate rows.

Usage::

    python manage.py load_sample_podcasts
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from podcasts.models import Podcast
from podcasts.services import (
    get_refresh_timeout_seconds,
    ingest_podcast,
)

SAMPLE_PODCASTS: list[dict[str, str]] = [
    {
        "id": "bbc_global_news",
        "title": "BBC Global News Podcast",
        "author": "BBC",
        "feed_url": "https://podcasts.files.bbci.co.uk/p02nq0gn.rss",
        "image_url": (
            "https://ichef.bbci.co.uk/images/ic/3000x3000/p09hxrtv.jpg"
        ),
        "language": "en",
    },
    {
        "id": "npr_up_first",
        "title": "NPR: Up First",
        "author": "NPR",
        "feed_url": "https://feeds.npr.org/510318/podcast.xml",
        "image_url": (
            "https://media.npr.org/assets/img/2023/03/01/"
            "up-first_podcast-tile_sq-7b46bef8770fb1d8aa9bce9f3f76bfa1"
            "78b3a398.jpg"
        ),
        "language": "en",
    },
]


class Command(BaseCommand):
    help = "Seed a small catalogue of public RSS feeds."

    def add_arguments(self, parser: object) -> None:
        """No arguments; kept for symmetry with other seed commands."""
        del parser

    def handle(self, *args: object, **options: object) -> None:
        for entry in SAMPLE_PODCASTS:
            podcast, created = Podcast.objects.update_or_create(
                id=entry["id"],
                defaults={
                    "title": entry["title"],
                    "author": entry["author"],
                    "feed_url": entry["feed_url"],
                    "image_url": entry["image_url"],
                    "language": entry["language"],
                    "is_active": True,
                },
            )
            self.stdout.write(
                f"{'created' if created else 'updated'} podcast "
                f"id={podcast.id} title={podcast.title!r}"
            )

        if (
            "--no-refresh" not in (args or ())
            and options.get("no_refresh", False) is False
        ):
            timeout = get_refresh_timeout_seconds()
            for podcast in Podcast.objects.filter(is_active=True).order_by(
                "id"
            ):
                report = ingest_podcast(podcast, timeout_seconds=timeout)
                self.stdout.write(
                    f"refreshed {podcast.id} seen={report.episodes_seen} "
                    f"created={report.episodes_created} "
                    f"updated={report.episodes_updated} "
                    f"error={report.error!r}"
                )
