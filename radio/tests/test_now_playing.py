"""Tests for now-playing enrichment with album / artwork.

Covers ``_enrich_with_album_artwork`` and the integration with
``refresh_now_playing``.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase

from radio.models import NowPlaying, Provider, Station
from radio.services import (
    _enrich_with_album_artwork,
    refresh_now_playing,
)

User = get_user_model()

DEEZER_HIT = {
    "data": [
        {
            "album": {
                "title": "Test Album",
                "cover_medium": (
                    "https://e-cdns.example.com/test/250x250.jpg"
                ),
            }
        }
    ]
}

DEEZER_MISS = {"data": []}


class EnrichWithAlbumArtworkTestCase(TestCase):
    """Unit coverage for ``_enrich_with_album_artwork``."""

    def test_returns_album_and_artwork_on_hit(self) -> None:
        with patch("radio.services.httpx.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__enter__.return_value
            mock_response = mock_client.get.return_value
            mock_response.status_code = 200
            mock_response.json.return_value = DEEZER_HIT

            result = _enrich_with_album_artwork("Artist", "Title")

        self.assertEqual(result.get("album"), "Test Album")
        self.assertEqual(
            result.get("artwork_url"),
            "https://e-cdns.example.com/test/250x250.jpg",
        )

    def test_returns_empty_on_miss(self) -> None:
        with patch("radio.services.httpx.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__enter__.return_value
            mock_response = mock_client.get.return_value
            mock_response.status_code = 200
            mock_response.json.return_value = DEEZER_MISS

            result = _enrich_with_album_artwork("Artist", "Title")

        self.assertEqual(result, {})

    def test_returns_empty_on_http_error(self) -> None:
        with patch("radio.services.httpx.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__enter__.return_value
            mock_client.get.side_effect = httpx.ReadTimeout("timed out")

            result = _enrich_with_album_artwork("Artist", "Title")

        self.assertEqual(result, {})

    def test_returns_empty_when_no_artist(self) -> None:
        result = _enrich_with_album_artwork("", "Title")
        self.assertEqual(result, {})

    def test_returns_empty_when_no_title(self) -> None:
        result = _enrich_with_album_artwork("Artist", "")
        self.assertEqual(result, {})

    def test_handles_non_json_response(self) -> None:
        with patch("radio.services.httpx.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__enter__.return_value
            mock_response = mock_client.get.return_value
            mock_response.status_code = 200
            mock_response.json.side_effect = ValueError

            result = _enrich_with_album_artwork("Artist", "Title")

        self.assertEqual(result, {})

    def test_handles_album_without_title(self) -> None:
        with patch("radio.services.httpx.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__enter__.return_value
            mock_response = mock_client.get.return_value
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "data": [{"album": {"foo": "bar"}}]
            }

            result = _enrich_with_album_artwork("Artist", "Title")

        self.assertEqual(result, {})


class RefreshNowPlayingIntegrationTestCase(TestCase):
    """Integration test for ``refresh_now_playing`` with enrichment."""

    def setUp(self) -> None:
        self.provider = Provider.objects.create(
            slug="test", name="Test", is_active=True
        )
        self.station = Station.objects.create(
            id="test_np",
            name="Test NP",
            provider=self.provider,
            country="UK",
            language="English",
            stream_url="https://example.test/test_np",
            metadata_url="https://example.test/metadata",
            is_active=True,
        )

    def test_enriches_now_playing_with_album_data(self) -> None:
        icy_payload = b"StreamTitle='Test Artist - Test Song';"

        with (
            patch("radio.services.httpx.Client") as mock_client_cls,
            patch(
                "radio.services._enrich_with_album_artwork",
                return_value={
                    "album": "Best Album",
                    "artwork_url": "https://e-cdns.example.com/art.jpg",
                },
            ),
        ):
            mock_http = mock_client_cls.return_value.__enter__.return_value
            mock_http.get.return_value.status_code = 200
            mock_http.get.return_value.content = icy_payload

            result = refresh_now_playing(self.station.id)

        self.assertEqual(result["attempted"], 1)
        self.assertEqual(result["updated"], 1)

        np_row = NowPlaying.objects.get(station=self.station)
        self.assertEqual(np_row.track_title, "Test Song")
        self.assertEqual(np_row.artist, "Test Artist")
        self.assertEqual(np_row.album, "Best Album")
        self.assertEqual(
            np_row.artwork_url, "https://e-cdns.example.com/art.jpg"
        )

    def test_skips_enrichment_when_icy_returns_empty(self) -> None:
        with (
            patch("radio.services.httpx.Client") as mock_client_cls,
            patch("radio.services._enrich_with_album_artwork") as mock_enrich,
        ):
            mock_http = mock_client_cls.return_value.__enter__.return_value
            mock_http.get.return_value.status_code = 404
            mock_http.get.return_value.content = b""

            refresh_now_playing(self.station.id)

        mock_enrich.assert_not_called()
