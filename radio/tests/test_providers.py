from unittest.mock import patch

import requests
from django.test import TestCase

from radio.providers.registry import (
    PROVIDER_REGISTRY,
    get_available_providers,
    get_provider,
    register_provider,
)
from radio.providers.somafm import SomaFMProvider
from radio.providers.tunein import TuneInProvider


class TestRegistry(TestCase):
    """Tests for provider registry."""

    def setUp(self) -> None:
        PROVIDER_REGISTRY.clear()

    def test_register_provider(self) -> None:
        """Test registering a provider."""
        register_provider("test", SomaFMProvider)
        self.assertIn("test", PROVIDER_REGISTRY)
        self.assertEqual(PROVIDER_REGISTRY["test"], SomaFMProvider)

    def test_get_provider(self) -> None:
        """Test getting a provider instance."""
        register_provider("test", SomaFMProvider)
        provider = get_provider("test")
        self.assertIsInstance(provider, SomaFMProvider)

    def test_get_provider_unknown_raises(self) -> None:
        """Test getting unknown provider raises ValueError."""
        with self.assertRaises(ValueError) as ctx:
            get_provider("nonexistent")
        self.assertEqual(str(ctx.exception), "Unknown provider: nonexistent")

    def test_get_available_providers(self) -> None:
        """Test getting list of available providers."""
        register_provider("prov1", SomaFMProvider)
        register_provider("prov2", TuneInProvider)
        providers = get_available_providers()
        self.assertIn("prov1", providers)
        self.assertIn("prov2", providers)


class TestSomaFMProvider(TestCase):
    """Tests for SomaFM provider."""

    def setUp(self) -> None:
        self.provider = SomaFMProvider()

    def test_get_stations(self) -> None:
        """Test getting stations returns list."""
        stations = self.provider.get_stations()
        self.assertIsInstance(stations, list)
        self.assertGreater(len(stations), 0)

    def test_get_stations_contains_expected_stations(self) -> None:
        """Test stations contain expected SomaFM stations."""
        stations = self.provider.get_stations()
        station_ids = [s["id"] for s in stations]
        self.assertIn("somafm_groovesalad", station_ids)
        self.assertIn("somafm_dronezone", station_ids)
        station = next(s for s in stations if s["id"] == "somafm_groovesalad")
        self.assertEqual(
            station["stream_url"],
            "https://ice5.somafm.com/groovesalad-128-mp3",
        )

    def test_get_stream_url_valid(self) -> None:
        """Test getting stream URL for valid station."""
        url = self.provider.get_stream_url("somafm_groovesalad")
        self.assertEqual(url, "https://ice5.somafm.com/groovesalad-128-mp3")

    def test_get_stream_url_invalid_raises(self) -> None:
        """Test getting stream URL for invalid station raises."""
        with self.assertRaises(ValueError) as ctx:
            self.provider.get_stream_url("invalid_station")
        self.assertEqual(
            str(ctx.exception), "Station not found: invalid_station"
        )

    def test_health_check_existing_station(self) -> None:
        """Test health check for existing station."""
        result = self.provider.health_check("somafm_groovesalad")
        self.assertTrue(result)

    def test_health_check_nonexistent_station(self) -> None:
        """Test health check for non-existent station."""
        result = self.provider.health_check("nonexistent")
        self.assertFalse(result)


class TestTuneInProvider(TestCase):
    """Tests for TuneIn provider."""

    def test_get_stations_fallback_no_api_key(self) -> None:
        """Test fallback stations when no API key."""
        provider = TuneInProvider(api_key=None)
        stations = provider.get_stations()
        self.assertIsInstance(stations, list)
        self.assertGreater(len(stations), 0)
        self.assertEqual(stations[0]["id"], "tunein_bbc_ws")

    def test_get_stations_fallback_empty_api_key(self) -> None:
        """Test fallback stations when API key is empty string."""
        provider = TuneInProvider(api_key="")
        stations = provider.get_stations()
        self.assertIsInstance(stations, list)
        self.assertGreater(len(stations), 0)

    def test_get_stations_api_failure_fallback(self) -> None:
        """Test fallback on API failure."""
        provider = TuneInProvider(api_key="test_key")
        with patch("radio.providers.tunein.requests.get") as mock_get:
            mock_get.side_effect = requests.RequestException("Network error")
            stations = provider.get_stations()
        self.assertIsInstance(stations, list)
        self.assertGreater(len(stations), 0)

    def test_get_stations_parses_api_response(self) -> None:
        """Test parsing API response."""
        provider = TuneInProvider(api_key="test_key")
        mock_response = {
            "Stations": [
                {
                    "id": "station1",
                    "name": "Test Station",
                    "genre": "Pop",
                    "country": "US",
                    "language": "English",
                    "stream_url": "http://test.com/stream",
                    "format": "MP3",
                    "bitrate": 128,
                }
            ]
        }
        with patch("radio.providers.tunein.requests.get") as mock_get:
            mock_response_obj = mock_get.return_value
            mock_response_obj.json.return_value = mock_response
            mock_response_obj.raise_for_status.return_value = None
            stations = provider.get_stations()
        self.assertEqual(len(stations), 1)
        self.assertEqual(stations[0]["id"], "tunein_station1")
        self.assertEqual(stations[0]["name"], "Test Station")

    def test_get_stream_url_valid(self) -> None:
        """Test getting stream URL for valid station."""
        provider = TuneInProvider()
        url = provider.get_stream_url("tunein_bbc_ws")
        self.assertEqual(
            url, "http://stream.live.vc.bbcmedia.co.uk/bbc_world_service"
        )

    def test_get_stream_url_invalid_raises(self) -> None:
        """Test getting stream URL for invalid station raises."""
        provider = TuneInProvider()
        with self.assertRaises(ValueError) as ctx:
            provider.get_stream_url("invalid_station")
        self.assertEqual(
            str(ctx.exception), "Station not found: invalid_station"
        )

    def test_health_check_existing_station(self) -> None:
        """Test health check for existing station."""
        provider = TuneInProvider()
        result = provider.health_check("tunein_bbc_ws")
        self.assertTrue(result)

    def test_health_check_nonexistent_station(self) -> None:
        """Test health check for non-existent station."""
        provider = TuneInProvider()
        result = provider.health_check("nonexistent")
        self.assertFalse(result)
