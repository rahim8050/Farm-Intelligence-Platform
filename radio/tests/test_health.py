"""Tests for the radio health endpoint and Celery health-check task."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
from django.test import TestCase, override_settings

from radio.models import Provider, Station, StationHealthCheck
from radio.tasks import check_all_stations_health


def _mock_client_factory(
    status_code: int = 200, raises: Exception | None = None
) -> MagicMock:
    """Build a mock httpx.Client that returns a fixed status code."""
    client = MagicMock(spec=httpx.Client)
    if raises is not None:
        client.head.side_effect = raises
        client.get.side_effect = raises
    else:
        client.head.return_value = httpx.Response(
            status_code, request=httpx.Request("HEAD", "https://x")
        )
        client.get.return_value = httpx.Response(
            status_code, request=httpx.Request("GET", "https://x")
        )
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    return client


class RadioHealthEndpointTestCase(TestCase):
    """Tests for ``GET /api/v1/radio/health/``."""

    def setUp(self) -> None:
        self.provider = Provider.objects.create(
            slug="bbc", name="BBC", is_active=True
        )
        Station.objects.create(
            id="bbc_1xtra",
            name="BBC 1Xtra",
            provider=self.provider,
            country="UK",
            language="English",
            stream_url="https://example.test/a",
            is_active=True,
            is_available=True,
        )
        StationHealthCheck.objects.create(
            station_id="bbc_1xtra",
            is_reachable=True,
            status_code=200,
            response_time_ms=10,
        )

    def test_health_endpoint_returns_envelope(self) -> None:
        response = self.client.get("/api/v1/radio/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], 0)
        self.assertIn("data", response.data)
        data = response.data["data"]
        self.assertEqual(data["stations_total"], 1)
        self.assertEqual(data["stations_available"], 1)
        self.assertEqual(data["stations_unavailable"], 0)
        self.assertEqual(data["status"], "healthy")
        self.assertIn("timestamp", data)

    def test_health_endpoint_reports_degraded(self) -> None:
        Station.objects.create(
            id="bbc_radio1",
            name="BBC Radio 1",
            provider=self.provider,
            country="UK",
            language="English",
            stream_url="https://example.test/b",
            is_active=True,
            is_available=False,
        )
        StationHealthCheck.objects.create(
            station_id="bbc_radio1",
            is_reachable=False,
            status_code=500,
            response_time_ms=20,
        )
        response = self.client.get("/api/v1/radio/health/")
        self.assertEqual(response.data["data"]["status"], "degraded")
        self.assertEqual(response.data["data"]["stations_total"], 2)
        self.assertEqual(response.data["data"]["stations_available"], 1)
        self.assertEqual(response.data["data"]["stations_unavailable"], 1)

    def test_health_endpoint_is_public(self) -> None:
        """No auth required."""
        response = self.client.get("/api/v1/radio/health/")
        self.assertEqual(response.status_code, 200)


class StationStreamHealthGateTestCase(TestCase):
    """Tests for the 503 path on ``StationStreamView`` when unavailable."""

    def setUp(self) -> None:
        self.provider = Provider.objects.create(
            slug="bbc", name="BBC", is_active=True
        )
        self.station = Station.objects.create(
            id="bbc_1xtra",
            name="BBC 1Xtra",
            provider=self.provider,
            country="UK",
            language="English",
            stream_url="https://example.test/stream",
            is_active=True,
        )

    def test_stream_returns_200_when_is_available_true(self) -> None:
        self.station.is_available = True
        self.station.save(update_fields=["is_available"])
        response = self.client.get("/api/v1/radio/stations/bbc_1xtra/stream/")
        self.assertEqual(response.status_code, 200)

    def test_stream_returns_503_when_is_available_false(self) -> None:
        self.station.is_available = False
        self.station.save(update_fields=["is_available"])
        response = self.client.get("/api/v1/radio/stations/bbc_1xtra/stream/")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["status"], 1)
        self.assertIsNone(response.data["data"])
        self.assertEqual(response.data["errors"]["station_id"], "bbc_1xtra")

    def test_stream_returns_200_when_never_checked(self) -> None:
        """Stations with ``is_available=None`` are not yet gated."""
        self.station.is_available = None
        self.station.save(update_fields=["is_available"])
        response = self.client.get("/api/v1/radio/stations/bbc_1xtra/stream/")
        self.assertEqual(response.status_code, 200)


class CheckAllStationsHealthTaskTestCase(TestCase):
    """Tests for ``radio.tasks.check_all_stations_health``."""

    def setUp(self) -> None:
        self.provider = Provider.objects.create(
            slug="bbc", name="BBC", is_active=True
        )
        Station.objects.create(
            id="bbc_1xtra",
            name="BBC 1Xtra",
            provider=self.provider,
            country="UK",
            language="English",
            stream_url="https://example.test/a",
            is_active=True,
        )
        Station.objects.create(
            id="bbc_radio1",
            name="BBC Radio 1",
            provider=self.provider,
            country="UK",
            language="English",
            stream_url="https://example.test/b",
            is_active=True,
        )

    @override_settings(RADIO_HEALTH_CHECK_TIMEOUT_SECONDS=1.0)
    def test_task_records_health_checks(self) -> None:
        mock_client = _mock_client_factory(status_code=200)
        with patch("radio.services.httpx.Client", return_value=mock_client):
            result = check_all_stations_health()
        self.assertEqual(result["checked"], 2)
        self.assertEqual(result["reachable"], 2)
        self.assertEqual(result["unreachable"], 0)
        self.assertEqual(StationHealthCheck.objects.count(), 2)
        for station_id in ("bbc_1xtra", "bbc_radio1"):
            station = Station.objects.get(id=station_id)
            self.assertTrue(station.is_available)
            self.assertIsNotNone(station.last_health_check_at)

    @override_settings(RADIO_HEALTH_CHECK_TIMEOUT_SECONDS=1.0)
    def test_task_records_failures(self) -> None:
        down = httpx.ConnectError("down", request=httpx.Request("HEAD", "x"))
        mock_client = _mock_client_factory(raises=down)
        with patch("radio.services.httpx.Client", return_value=mock_client):
            result = check_all_stations_health()
        self.assertEqual(result["checked"], 2)
        self.assertEqual(result["reachable"], 0)
        self.assertEqual(result["unreachable"], 2)
        for station_id in ("bbc_1xtra", "bbc_radio1"):
            station = Station.objects.get(id=station_id)
            self.assertFalse(station.is_available)

    @override_settings(RADIO_HEALTH_CHECK_TIMEOUT_SECONDS=1.0)
    def test_task_is_idempotent(self) -> None:
        """Running the task twice creates a second row per station."""
        mock_client = _mock_client_factory(status_code=200)
        with patch("radio.services.httpx.Client", return_value=mock_client):
            check_all_stations_health()
            check_all_stations_health()
        self.assertEqual(StationHealthCheck.objects.count(), 4)
