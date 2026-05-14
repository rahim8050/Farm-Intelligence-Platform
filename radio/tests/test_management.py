from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from radio.models import Provider, Station


class LoadStationsCommandTestCase(TestCase):
    """Tests for load_stations management command."""

    def test_load_stations_creates_provider(self) -> None:
        """Test command creates BBC provider."""
        out = StringIO()
        call_command("load_stations", stdout=out)
        provider = Provider.objects.filter(slug="bbc").first()
        self.assertIsNotNone(provider)
        self.assertEqual(provider.name, "BBC")

    def test_load_stations_creates_station(self) -> None:
        """Test command creates BBC 1Xtra station."""
        call_command("load_stations")
        station = Station.objects.filter(id="bbc_1xtra").first()
        self.assertIsNotNone(station)
        self.assertEqual(station.name, "BBC 1Xtra")
        self.assertEqual(
            station.stream_url,
            "http://as-hls-ww-live.akamaized.net/pool_92079267/live/ww/bbc_1xtra/bbc_1xtra.isml/bbc_1xtra-audio%3d96000.norewind.m3u8",
        )

    def test_load_stations_idempotent(self) -> None:
        """Test command is idempotent - running again doesn't duplicate."""
        call_command("load_stations")
        call_command("load_stations")
        stations = Station.objects.filter(id="bbc_1xtra")
        self.assertEqual(stations.count(), 1)

    def test_load_stations_output(self) -> None:
        """Test command outputs correct messages."""
        out = StringIO()
        call_command("load_stations", stdout=out)
        output = out.getvalue()
        self.assertIn("Provider: BBC", output)
        self.assertIn("Created station: BBC 1Xtra", output)
        self.assertIn("Radio stations loaded successfully", output)
