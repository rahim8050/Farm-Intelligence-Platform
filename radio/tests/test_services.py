"""Tests for the radio service layer (health-check logic)."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
from django.test import TestCase

from radio.models import Provider, Station, StationHealthCheck
from radio.services import (
    HealthProbeResult,
    probe_all_active_stations,
    probe_and_record,
    probe_station,
    record_probe_result,
    summarize_health,
)


def _make_response(status_code: int) -> httpx.Response:
    """Build an httpx.Response with the given status code."""
    return httpx.Response(
        status_code, request=httpx.Request("HEAD", "https://x")
    )


def _make_client(
    *,
    head_status: int = 200,
    get_status: int | None = None,
    raises: Exception | None = None,
) -> httpx.Client:
    """Build a mock httpx.Client with predetermined behaviour."""
    client = MagicMock(spec=httpx.Client)
    if raises is not None:
        client.head.side_effect = raises
        client.get.side_effect = raises
    else:
        head_resp = _make_response(head_status)
        client.head.return_value = head_resp
        if get_status is not None:
            client.get.return_value = _make_response(get_status)
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    return client


class ProbeStationTestCase(TestCase):
    """Tests for :func:`radio.services.probe_station`."""

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

    def test_probe_returns_reachable_on_2xx(self) -> None:
        client = _make_client(head_status=200)
        result = probe_station(
            self.station, timeout_seconds=2.0, client=client
        )
        self.assertIsInstance(result, HealthProbeResult)
        self.assertEqual(result.station_id, "bbc_1xtra")
        self.assertTrue(result.is_reachable)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.error_message, "")

    def test_probe_returns_unreachable_on_5xx(self) -> None:
        client = _make_client(head_status=503)
        result = probe_station(
            self.station, timeout_seconds=2.0, client=client
        )
        self.assertFalse(result.is_reachable)
        self.assertEqual(result.status_code, 503)
        self.assertIn("HTTP 503", result.error_message)

    def test_probe_falls_back_to_get_on_405(self) -> None:
        client = _make_client(head_status=405, get_status=206)
        result = probe_station(
            self.station, timeout_seconds=2.0, client=client
        )
        self.assertTrue(result.is_reachable)
        self.assertEqual(result.status_code, 206)
        client.get.assert_called_once()

    def test_probe_records_network_error(self) -> None:
        boom = httpx.ConnectError("boom", request=httpx.Request("HEAD", "x"))
        client = _make_client(raises=boom)
        result = probe_station(
            self.station, timeout_seconds=2.0, client=client
        )
        self.assertFalse(result.is_reachable)
        self.assertIsNone(result.status_code)
        self.assertIn("ConnectError", result.error_message)
        self.assertIn("boom", result.error_message)


class RecordProbeResultTestCase(TestCase):
    """Tests for :func:`radio.services.record_probe_result`."""

    def setUp(self) -> None:
        self.provider = Provider.objects.create(
            slug="bbc", name="BBC", is_active=True
        )
        self.station = Station.objects.create(
            id="bbc_radio1",
            name="BBC Radio 1",
            provider=self.provider,
            country="UK",
            language="English",
            stream_url="https://example.test/stream",
            is_active=True,
        )

    def test_record_persists_row_and_updates_station(self) -> None:
        result = HealthProbeResult(
            station_id=self.station.id,
            is_reachable=True,
            status_code=200,
            response_time_ms=42,
            error_message="",
        )
        check = record_probe_result(self.station, result)

        self.station.refresh_from_db()
        self.assertTrue(self.station.is_available)
        self.assertIsNotNone(self.station.last_health_check_at)
        self.assertEqual(check.station_id, self.station.id)
        self.assertEqual(check.status_code, 200)
        self.assertTrue(check.is_reachable)
        self.assertEqual(
            StationHealthCheck.objects.filter(station=self.station).count(), 1
        )

    def test_record_marks_unavailable_on_failure(self) -> None:
        result = HealthProbeResult(
            station_id=self.station.id,
            is_reachable=False,
            status_code=502,
            response_time_ms=120,
            error_message="HTTP 502",
        )
        record_probe_result(self.station, result)

        self.station.refresh_from_db()
        self.assertFalse(self.station.is_available)
        self.assertEqual(
            StationHealthCheck.objects.filter(
                station=self.station, is_reachable=False
            ).count(),
            1,
        )


class ProbeAndRecordTestCase(TestCase):
    """Tests for the combined probe-and-record helper."""

    def setUp(self) -> None:
        self.provider = Provider.objects.create(
            slug="bbc", name="BBC", is_active=True
        )
        self.station = Station.objects.create(
            id="bbc_radio2",
            name="BBC Radio 2",
            provider=self.provider,
            country="UK",
            language="English",
            stream_url="https://example.test/stream",
            is_active=True,
        )

    def test_probe_and_record_updates_station(self) -> None:
        client = _make_client(head_status=200)
        result = probe_and_record(
            self.station, timeout_seconds=2.0, client=client
        )
        self.assertTrue(result.is_reachable)
        self.station.refresh_from_db()
        self.assertTrue(self.station.is_available)


class ProbeAllActiveStationsTestCase(TestCase):
    """Tests for the bulk probe used by the Celery task."""

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
        Station.objects.create(
            id="bbc_inactive",
            name="BBC Inactive",
            provider=self.provider,
            country="UK",
            language="English",
            stream_url="https://example.test/c",
            is_active=False,
        )

    def test_iterates_only_active_stations(self) -> None:
        client = _make_client(head_status=200)
        results = probe_all_active_stations(timeout_seconds=2.0, client=client)
        ids = sorted(r.station_id for r in results)
        self.assertEqual(ids, ["bbc_1xtra", "bbc_radio1"])

    def test_records_each_result(self) -> None:
        client = _make_client(head_status=200)
        probe_all_active_stations(timeout_seconds=2.0, client=client)
        self.assertEqual(StationHealthCheck.objects.count(), 2)
        for station_id in ("bbc_1xtra", "bbc_radio1"):
            station = Station.objects.get(id=station_id)
            self.assertTrue(station.is_available)


class SummarizeHealthTestCase(TestCase):
    """Tests for :func:`radio.services.summarize_health`."""

    def setUp(self) -> None:
        self.provider = Provider.objects.create(
            slug="bbc", name="BBC", is_active=True
        )
        self.station_a = Station.objects.create(
            id="bbc_1xtra",
            name="BBC 1Xtra",
            provider=self.provider,
            country="UK",
            language="English",
            stream_url="https://example.test/a",
            is_active=True,
            is_available=True,
        )
        self.station_b = Station.objects.create(
            id="bbc_radio1",
            name="BBC Radio 1",
            provider=self.provider,
            country="UK",
            language="English",
            stream_url="https://example.test/b",
            is_active=True,
            is_available=False,
        )
        self.station_unchecked = Station.objects.create(
            id="somafm_unknown",
            name="SomaFM Unchecked",
            provider=self.provider,
            country="US",
            language="English",
            stream_url="https://example.test/c",
            is_active=True,
            is_available=None,
        )

    def test_summarize_uses_db_state(self) -> None:
        summary = summarize_health()
        self.assertEqual(summary["stations_total"], 3)
        self.assertEqual(summary["stations_available"], 1)
        self.assertEqual(summary["stations_unavailable"], 1)
        self.assertEqual(summary["stations_unchecked"], 1)
        self.assertEqual(summary["status"], "degraded")

    def test_summarize_marks_healthy_when_all_reachable(self) -> None:
        self.station_b.is_available = True
        self.station_b.save(update_fields=["is_available"])
        self.station_unchecked.is_available = True
        self.station_unchecked.save(update_fields=["is_available"])
        summary = summarize_health()
        self.assertEqual(summary["status"], "healthy")
        self.assertEqual(summary["stations_available"], 3)
        self.assertEqual(summary["stations_unavailable"], 0)

    def test_summarize_marks_unhealthy_when_all_checked_are_down(self) -> None:
        self.station_unchecked.delete()
        self.station_a.is_available = False
        self.station_a.save(update_fields=["is_available"])
        summary = summarize_health()
        self.assertEqual(summary["status"], "unhealthy")
        self.assertEqual(summary["stations_total"], 2)
        self.assertEqual(summary["stations_available"], 0)
        self.assertEqual(summary["stations_unavailable"], 2)

    def test_summarize_marks_degraded_when_no_stations(self) -> None:
        Station.objects.all().delete()
        summary = summarize_health()
        self.assertEqual(summary["status"], "degraded")
        self.assertEqual(summary["stations_total"], 0)

    def test_summarize_includes_timestamp(self) -> None:
        summary = summarize_health()
        self.assertIn("timestamp", summary)
