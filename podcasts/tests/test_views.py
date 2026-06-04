"""Tests for the podcast API endpoints."""

from __future__ import annotations

import secrets
from unittest.mock import MagicMock, patch

import httpx
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from podcasts.models import Podcast, PodcastEpisode

User = get_user_model()


def _podcast(**overrides: object) -> Podcast:
    defaults: dict[str, object] = {
        "id": "sample",
        "title": "Sample Show",
        "author": "Sample Author",
        "feed_url": "https://example.test/feed.rss",
        "is_active": True,
    }
    defaults.update(overrides)
    return Podcast.objects.create(**defaults)  # type: ignore[arg-type]


def _episode(podcast: Podcast, **overrides: object) -> PodcastEpisode:
    defaults: dict[str, object] = {
        "podcast": podcast,
        "guid": "ep-1",
        "title": "Episode 1",
        "description": "Description",
        "audio_url": "https://example.test/ep1.mp3",
        "audio_mime_type": "audio/mpeg",
        "duration_seconds": 600,
    }
    defaults.update(overrides)
    return PodcastEpisode.objects.create(**defaults)


SAMPLE_RSS = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>Sample Show</title>
    <item>
      <title>First Episode</title>
      <guid isPermaLink="false">ep-1</guid>
      <enclosure
        url="https://example.test/ep1.mp3"
        type="audio/mpeg"
        length="1"
      />
    </item>
  </channel>
</rss>
"""


def _ok_client(content: bytes = SAMPLE_RSS) -> MagicMock:
    client = MagicMock(spec=httpx.Client)
    response = httpx.Response(
        200,
        content=content,
        request=httpx.Request("GET", "https://example.test/feed.rss"),
    )
    client.get.return_value = response
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    return client


class PodcastListEndpointTestCase(APITestCase):
    def setUp(self) -> None:
        self.podcast = _podcast()

    def test_returns_envelope_with_list(self) -> None:
        response = self.client.get("/api/v1/podcasts/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], 0)
        self.assertEqual(len(response.data["data"]), 1)
        self.assertEqual(response.data["data"][0]["id"], "sample")

    def test_inactive_podcasts_excluded(self) -> None:
        _podcast(id="inactive", is_active=False)
        response = self.client.get("/api/v1/podcasts/")
        self.assertEqual(len(response.data["data"]), 1)


class PodcastDetailEndpointTestCase(APITestCase):
    def setUp(self) -> None:
        self.podcast = _podcast()

    def test_returns_podcast(self) -> None:
        response = self.client.get("/api/v1/podcasts/sample/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["id"], "sample")

    def test_returns_404_for_unknown_podcast(self) -> None:
        response = self.client.get("/api/v1/podcasts/missing/")
        self.assertEqual(response.status_code, 404)


class PodcastEpisodesEndpointTestCase(APITestCase):
    def setUp(self) -> None:
        self.podcast = _podcast()
        for i in range(3):
            _episode(
                self.podcast,
                guid=f"ep-{i}",
                title=f"Episode {i}",
            )

    def test_lists_episodes(self) -> None:
        response = self.client.get("/api/v1/podcasts/sample/episodes/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["data"]), 3)

    def test_limit_query_param(self) -> None:
        response = self.client.get(
            "/api/v1/podcasts/sample/episodes/", {"limit": "2"}
        )
        self.assertEqual(len(response.data["data"]), 2)

    def test_rejects_invalid_limit(self) -> None:
        response = self.client.get(
            "/api/v1/podcasts/sample/episodes/", {"limit": "abc"}
        )
        self.assertEqual(response.status_code, 400)

    def test_rejects_out_of_range_limit(self) -> None:
        response = self.client.get(
            "/api/v1/podcasts/sample/episodes/", {"limit": "9999"}
        )
        self.assertEqual(response.status_code, 400)


class PodcastEpisodeStreamEndpointTestCase(APITestCase):
    def setUp(self) -> None:
        self.podcast = _podcast()
        self.episode = _episode(self.podcast)

    def test_returns_stream_payload(self) -> None:
        response = self.client.get(
            f"/api/v1/podcasts/episodes/{self.episode.id}/stream/"
        )
        self.assertEqual(response.status_code, 200)
        data = response.data["data"]
        self.assertEqual(data["audio_url"], "https://example.test/ep1.mp3")
        self.assertEqual(data["format"], "audio/mpeg")
        self.assertEqual(data["duration_seconds"], 600)
        self.assertEqual(data["episode_title"], "Episode 1")
        self.assertEqual(data["podcast_id"], "sample")

    def test_returns_404_for_unknown_episode(self) -> None:
        response = self.client.get("/api/v1/podcasts/episodes/999/stream/")
        self.assertEqual(response.status_code, 404)


class PodcastRefreshEndpointTestCase(APITestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="alice", password=secrets.token_urlsafe(12)
        )
        self.podcast = _podcast()

    def test_unauthenticated_returns_401(self) -> None:
        response = self.client.post("/api/v1/podcasts/sample/refresh/")
        self.assertEqual(response.status_code, 401)

    def test_authenticated_refresh_creates_episodes(self) -> None:
        self.client.force_authenticate(user=self.user)
        with patch(
            "podcasts.services.httpx.Client", return_value=_ok_client()
        ):
            response = self.client.post("/api/v1/podcasts/sample/refresh/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["episodes_created"], 1)
        self.assertEqual(
            PodcastEpisode.objects.filter(podcast=self.podcast).count(), 1
        )

    def test_refresh_returns_404_for_unknown_podcast(self) -> None:
        self.client.force_authenticate(user=self.user)
        response = self.client.post("/api/v1/podcasts/missing/refresh/")
        self.assertEqual(response.status_code, 404)
