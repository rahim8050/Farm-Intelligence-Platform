from django.test import TestCase

from radio.models import Provider, Station


class RadioAPITestCase(TestCase):
    """Tests for Radio API endpoints."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.provider = Provider.objects.create(
            slug="bbc",
            name="BBC",
            website_url="https://www.bbc.co.uk",
            is_active=True,
        )
        cls.station = Station.objects.create(
            id="bbc_1xtra",
            name="BBC 1Xtra",
            provider=cls.provider,
            genre="Hip Hop",
            country="UK",
            language="English",
            stream_url="http://stream.live.vc.bbcmedia.co.uk/bbc_1xtra",
            format="MP3",
            bitrate=128,
            is_active=True,
        )

    def test_station_list_returns_active_stations(self) -> None:
        """Test station list endpoint returns active stations."""
        response = self.client.get("/api/v1/radio/stations/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], 0)
        self.assertIn("data", response.data)
        self.assertEqual(len(response.data["data"]), 1)
        self.assertEqual(response.data["data"][0]["id"], "bbc_1xtra")

    def test_station_detail_returns_station(self) -> None:
        """Test station detail endpoint returns station."""
        response = self.client.get("/api/v1/radio/stations/bbc_1xtra/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], 0)
        self.assertEqual(response.data["data"]["id"], "bbc_1xtra")
        self.assertIn("stream_url", response.data["data"])

    def test_station_detail_not_found(self) -> None:
        """Test station detail returns 404 for non-existent station."""
        response = self.client.get("/api/v1/radio/stations/nonexistent/")
        self.assertEqual(response.status_code, 404)

    def test_station_stream_returns_stream_url(self) -> None:
        """Test station stream endpoint returns stream URL."""
        response = self.client.get("/api/v1/radio/stations/bbc_1xtra/stream/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], 0)
        self.assertIn("stream_url", response.data["data"])
        self.assertEqual(
            response.data["data"]["stream_url"],
            "http://stream.live.vc.bbcmedia.co.uk/bbc_1xtra",
        )
        self.assertEqual(response.data["data"]["format"], "MP3")
        self.assertEqual(response.data["data"]["bitrate"], 128)

    def test_provider_list_returns_active_providers(self) -> None:
        """Test provider list endpoint returns active providers."""
        response = self.client.get("/api/v1/radio/providers/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], 0)
        self.assertEqual(len(response.data["data"]), 1)
        self.assertEqual(response.data["data"][0]["slug"], "bbc")

    def test_inactive_station_not_in_list(self) -> None:
        """Test inactive stations are not returned in list."""
        Station.objects.create(
            id="inactive_station",
            name="Inactive Station",
            provider=self.provider,
            country="UK",
            language="English",
            stream_url="http://example.com/stream",
            is_active=False,
        )
        response = self.client.get("/api/v1/radio/stations/")
        self.assertEqual(len(response.data["data"]), 1)
