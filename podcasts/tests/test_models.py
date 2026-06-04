"""Tests for the podcast models."""

from __future__ import annotations

from django.test import TestCase

from podcasts.models import Podcast, PodcastEpisode


class PodcastModelTestCase(TestCase):
    def setUp(self) -> None:
        self.podcast = Podcast.objects.create(
            id="bbc_global_news",
            title="BBC Global News Podcast",
            author="BBC",
            feed_url="https://example.test/feed.rss",
            is_active=True,
        )

    def test_podcast_str_returns_title(self) -> None:
        self.assertEqual(str(self.podcast), "BBC Global News Podcast")

    def test_episode_str_includes_podcast_id(self) -> None:
        ep = PodcastEpisode.objects.create(
            podcast=self.podcast,
            guid="ep-1",
            title="Episode 1",
            audio_url="https://example.test/ep1.mp3",
        )
        self.assertEqual(str(ep), "bbc_global_news: Episode 1")

    def test_unique_guid_per_podcast(self) -> None:
        from django.db import IntegrityError, transaction

        PodcastEpisode.objects.create(
            podcast=self.podcast,
            guid="ep-1",
            title="Episode 1",
            audio_url="https://example.test/ep1.mp3",
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PodcastEpisode.objects.create(
                    podcast=self.podcast,
                    guid="ep-1",
                    title="Episode 1 duplicate",
                    audio_url="https://example.test/ep1.mp3",
                )

    def test_same_guid_allowed_across_podcasts(self) -> None:
        other = Podcast.objects.create(
            id="other_podcast",
            title="Other",
            feed_url="https://example.test/other.rss",
            is_active=True,
        )
        PodcastEpisode.objects.create(
            podcast=self.podcast,
            guid="shared",
            title="Ep A",
            audio_url="https://example.test/a.mp3",
        )
        # Second podcast may reuse the same guid without conflict.
        PodcastEpisode.objects.create(
            podcast=other,
            guid="shared",
            title="Ep B",
            audio_url="https://example.test/b.mp3",
        )
        self.assertEqual(
            PodcastEpisode.objects.filter(guid="shared").count(), 2
        )
