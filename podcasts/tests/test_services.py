"""Tests for the podcast service layer (feed ingestion)."""

from __future__ import annotations

from datetime import UTC
from unittest.mock import MagicMock, patch

import httpx
from django.test import TestCase, override_settings

from podcasts.models import Podcast, PodcastEpisode
from podcasts.services import (
    PodcastIngestionError,
    _coerce_duration,
    _coerce_published,
    fetch_feed,
    ingest_podcast,
    parse_feed_bytes,
)

SAMPLE_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>Sample Show</title>
    <link>https://example.test/</link>
    <description>A test feed.</description>
    <item>
      <title>First Episode</title>
      <guid isPermaLink="false">ep-1</guid>
      <pubDate>Mon, 02 Jun 2026 09:00:00 +0000</pubDate>
      <itunes:duration>00:15:30</itunes:duration>
      <enclosure
        url="https://example.test/ep1.mp3"
        type="audio/mpeg"
        length="1234"
      />
      <description>First description.</description>
    </item>
    <item>
      <title>Second Episode</title>
      <guid isPermaLink="false">ep-2</guid>
      <pubDate>Tue, 03 Jun 2026 09:00:00 +0000</pubDate>
      <itunes:duration>930</itunes:duration>
      <enclosure
        url="https://example.test/ep2.mp3"
        type="audio/mpeg"
        length="1234"
      />
      <description>Second description.</description>
    </item>
    <item>
      <title>Broken</title>
      <!-- no guid, no enclosure: must be skipped silently -->
    </item>
  </channel>
</rss>
"""


def _mock_client_factory(
    content: bytes = b"", status_code: int = 200
) -> MagicMock:
    client = MagicMock(spec=httpx.Client)
    response = httpx.Response(
        status_code,
        content=content,
        request=httpx.Request("GET", "https://example.test/feed.rss"),
    )
    client.get.return_value = response
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    return client


class FeedParsingTestCase(TestCase):
    def test_coerce_duration_seconds(self) -> None:
        self.assertEqual(_coerce_duration("930"), 930)

    def test_coerce_duration_mmss(self) -> None:
        self.assertEqual(_coerce_duration("15:30"), 930)

    def test_coerce_duration_hms(self) -> None:
        self.assertEqual(_coerce_duration("01:15:30"), 4530)

    def test_coerce_duration_invalid(self) -> None:
        self.assertIsNone(_coerce_duration("not a number"))
        self.assertIsNone(_coerce_duration(None))
        self.assertIsNone(_coerce_duration(""))

    def test_coerce_published_rfc822(self) -> None:

        result = _coerce_published("Mon, 02 Jun 2026 09:00:00 +0000")
        assert result is not None
        self.assertEqual(
            result.astimezone(UTC).date().isoformat(), "2026-06-02"
        )

    def test_coerce_published_invalid(self) -> None:
        self.assertIsNone(_coerce_published("not a date"))
        self.assertIsNone(_coerce_published(None))

    def test_parse_feed_bytes_returns_entries(self) -> None:
        parsed = parse_feed_bytes(SAMPLE_RSS)
        self.assertEqual(len(parsed.entries), 3)


class FetchFeedTestCase(TestCase):
    def test_fetch_feed_returns_content(self) -> None:
        mock_client = _mock_client_factory(content=b"<rss/>")
        content = fetch_feed(
            "https://example.test/feed.rss",
            timeout_seconds=1.0,
            client=mock_client,
        )
        self.assertEqual(content, b"<rss/>")

    def test_fetch_feed_raises_on_http_error(self) -> None:
        mock_client = _mock_client_factory(status_code=500)
        with self.assertRaises(PodcastIngestionError):
            fetch_feed(
                "https://example.test/feed.rss",
                timeout_seconds=1.0,
                client=mock_client,
            )

    def test_fetch_feed_raises_on_network_error(self) -> None:
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.get.side_effect = httpx.ConnectError(
            "down", request=httpx.Request("GET", "https://example.test")
        )
        with self.assertRaises(PodcastIngestionError):
            fetch_feed(
                "https://example.test/feed.rss",
                timeout_seconds=1.0,
                client=mock_client,
            )


class IngestPodcastTestCase(TestCase):
    def setUp(self) -> None:
        self.podcast = Podcast.objects.create(
            id="sample",
            title="Sample Show",
            feed_url="https://example.test/feed.rss",
            is_active=True,
        )

    @override_settings(PODCASTS_REFRESH_TIMEOUT_SECONDS=1.0)
    def test_ingest_creates_episodes(self) -> None:
        mock_client = _mock_client_factory(content=SAMPLE_RSS)
        with patch("podcasts.services.httpx.Client", return_value=mock_client):
            report = ingest_podcast(self.podcast, timeout_seconds=1.0)
        self.assertEqual(report.error, "")
        self.assertEqual(report.episodes_seen, 3)
        self.assertEqual(report.episodes_created, 2)
        self.assertEqual(report.episodes_updated, 0)
        self.assertEqual(
            PodcastEpisode.objects.filter(podcast=self.podcast).count(), 2
        )
        self.podcast.refresh_from_db()
        self.assertEqual(self.podcast.last_refresh_status, "ok")

    @override_settings(PODCASTS_REFRESH_TIMEOUT_SECONDS=1.0)
    def test_ingest_updates_existing_episodes(self) -> None:
        PodcastEpisode.objects.create(
            podcast=self.podcast,
            guid="ep-1",
            title="OLD",
            audio_url="https://example.test/ep1.mp3",
            description="OLD",
        )
        mock_client = _mock_client_factory(content=SAMPLE_RSS)
        with patch("podcasts.services.httpx.Client", return_value=mock_client):
            report = ingest_podcast(self.podcast, timeout_seconds=1.0)
        self.assertEqual(report.episodes_created, 1)
        self.assertEqual(report.episodes_updated, 1)
        ep = PodcastEpisode.objects.get(podcast=self.podcast, guid="ep-1")
        self.assertEqual(ep.title, "First Episode")
        self.assertEqual(ep.description, "First description.")
        self.assertEqual(ep.duration_seconds, 930)
        self.assertEqual(ep.audio_mime_type, "audio/mpeg")

    @override_settings(PODCASTS_REFRESH_TIMEOUT_SECONDS=1.0)
    def test_ingest_marks_failure_on_http_error(self) -> None:
        mock_client = _mock_client_factory(status_code=503)
        with patch("podcasts.services.httpx.Client", return_value=mock_client):
            report = ingest_podcast(self.podcast, timeout_seconds=1.0)
        self.assertNotEqual(report.error, "")
        self.assertEqual(report.episodes_created, 0)
        self.podcast.refresh_from_db()
        self.assertEqual(self.podcast.last_refresh_status, "error")
        self.assertIn("503", self.podcast.last_refresh_error)

    @override_settings(PODCASTS_REFRESH_TIMEOUT_SECONDS=1.0)
    def test_ingest_handles_unparseable_feed(self) -> None:
        mock_client = _mock_client_factory(content=b"not-xml-but-ok")
        with patch("podcasts.services.httpx.Client", return_value=mock_client):
            report = ingest_podcast(self.podcast, timeout_seconds=1.0)
        # feedparser is permissive; empty entries is fine.
        self.assertEqual(report.error, "")
        self.assertEqual(report.episodes_created, 0)
