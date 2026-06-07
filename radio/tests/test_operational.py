"""Tests for radio operational hardening (Phase 6).

Covers:
- ``radio.metrics.observe_request`` and the ``@timed`` decorator.
- ``radio.services.list_active_stations_cached`` + cache
  invalidation on ``Station`` / ``Provider`` writes.
- ``StationListView`` ``?genre=`` and ``?country=`` query filters.
- ``StationHealthHistoryView`` per-station audit trail.
- Cache invalidation signals on ``Station`` and ``Provider``.
"""

from __future__ import annotations

from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from radio.metrics import (
    ERROR_STATUS_THRESHOLD,
    radio_api_request_errors_total,
    radio_api_request_latency_seconds,
)
from radio.models import (
    Provider,
    Station,
    StationHealthCheck,
)
from radio.services import (
    invalidate_station_list_cache,
    list_active_stations_cached,
)


def _make_provider(slug: str = "bbc") -> Provider:
    return Provider.objects.create(
        slug=slug, name=slug.upper(), is_active=True
    )


def _make_station(
    provider: Provider,
    id_: str,
    *,
    genre: str = "Pop",
    country: str = "UK",
) -> Station:
    return Station.objects.create(
        id=id_,
        name=id_,
        provider=provider,
        genre=genre,
        country=country,
        language="English",
        stream_url=f"https://example.test/{id_}",
        is_active=True,
    )


class ObserveRequestTestCase(TestCase):
    """Direct tests for the Prometheus request hook."""

    def setUp(self) -> None:
        cache.clear()

    def test_observe_request_records_latency(self) -> None:
        from radio.metrics import observe_request

        before = radio_api_request_latency_seconds.labels(
            endpoint="unit.test", method="GET"
        )._sum.get()  # type: ignore[attr-defined]
        observe_request("unit.test", "GET", 200, 0.123)
        after = radio_api_request_latency_seconds.labels(
            endpoint="unit.test", method="GET"
        )._sum.get()  # type: ignore[attr-defined]
        self.assertGreater(after, before)

    def test_observe_request_increments_error_counter_on_5xx(self) -> None:
        from radio.metrics import observe_request

        before = radio_api_request_errors_total.labels(
            endpoint="unit.err", method="POST", status_code="503"
        )._value.get()  # type: ignore[attr-defined]
        observe_request("unit.err", "POST", 503, 0.5)
        after = radio_api_request_errors_total.labels(
            endpoint="unit.err", method="POST", status_code="503"
        )._value.get()  # type: ignore[attr-defined]
        self.assertEqual(after - before, 1)

    def test_observe_request_does_not_count_2xx_as_error(self) -> None:
        from radio.metrics import observe_request

        before = radio_api_request_errors_total.labels(
            endpoint="unit.ok", method="GET", status_code="200"
        )._value.get()  # type: ignore[attr-defined]
        observe_request("unit.ok", "GET", 200, 0.05)
        after = radio_api_request_errors_total.labels(
            endpoint="unit.ok", method="GET", status_code="200"
        )._value.get()  # type: ignore[attr-defined]
        self.assertEqual(after - before, 0)

    def test_threshold_is_400(self) -> None:
        self.assertEqual(ERROR_STATUS_THRESHOLD, 400)


class StationListCacheTestCase(TestCase):
    """Coverage for the station-list cache helper + invalidation."""

    def setUp(self) -> None:
        cache.clear()
        self.provider = _make_provider()

    def test_first_call_populates_cache(self) -> None:
        _make_station(self.provider, "bbc_1xtra")
        _make_station(self.provider, "bbc_radio1")
        invalidate_station_list_cache()
        stations = list_active_stations_cached()
        self.assertEqual(len(stations), 2)
        cached = cache.get("radio:stations:all")
        self.assertIsNotNone(cached)
        self.assertEqual(len(cached), 2)

    def test_cache_invalidated_on_station_save(self) -> None:
        s = _make_station(self.provider, "bbc_1xtra")
        list_active_stations_cached()
        self.assertIsNotNone(cache.get("radio:stations:all"))
        s.name = "BBC 1Xtra (renamed)"
        s.save()
        self.assertIsNone(cache.get("radio:stations:all"))

    def test_cache_invalidated_on_provider_save(self) -> None:
        list_active_stations_cached()
        self.assertIsNotNone(cache.get("radio:stations:all"))
        self.provider.name = "BBC Renamed"
        self.provider.save()
        self.assertIsNone(cache.get("radio:stations:all"))


class StationListFilterTestCase(TestCase):
    """Coverage for the ``?genre=`` and ``?country=`` query filters."""

    def setUp(self) -> None:
        cache.clear()
        self.provider = _make_provider()
        self.station_a = _make_station(
            self.provider, "bbc_1xtra", genre="Hip Hop", country="UK"
        )
        self.station_b = _make_station(
            self.provider, "bbc_radio1", genre="Pop", country="UK"
        )
        self.station_c = _make_station(
            self.provider, "kexp", genre="Indie", country="US"
        )

    def test_unfiltered_returns_all(self) -> None:
        response = APIClient().get(reverse("station-list"))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["data"]), 3)

    def test_filter_by_genre(self) -> None:
        response = APIClient().get(reverse("station-list"), {"genre": "Pop"})
        self.assertEqual(response.status_code, 200)
        ids = [s["id"] for s in response.json()["data"]]
        self.assertEqual(ids, ["bbc_radio1"])

    def test_filter_by_country(self) -> None:
        response = APIClient().get(reverse("station-list"), {"country": "US"})
        self.assertEqual(response.status_code, 200)
        ids = [s["id"] for s in response.json()["data"]]
        self.assertEqual(ids, ["kexp"])

    def test_filter_combined(self) -> None:
        response = APIClient().get(
            reverse("station-list"), {"genre": "Hip Hop", "country": "UK"}
        )
        self.assertEqual(response.status_code, 200)
        ids = [s["id"] for s in response.json()["data"]]
        self.assertEqual(ids, ["bbc_1xtra"])


class StationHealthHistoryTestCase(TestCase):
    """Coverage for ``GET /api/v1/radio/stations/<id>/health/``."""

    def setUp(self) -> None:
        self.provider = _make_provider()
        self.station = _make_station(self.provider, "bbc_1xtra")
        now = timezone.now()
        for i in range(5):
            StationHealthCheck.objects.create(
                station=self.station,
                checked_at=now - timedelta(minutes=i * 10),
                is_reachable=(i % 2 == 0),
                response_time_ms=100 + i,
                status_code=200 if i % 2 == 0 else 500,
            )

    def test_history_returns_recent_checks_newest_first(self) -> None:
        response = APIClient().get(
            reverse("station-health-history", args=["bbc_1xtra"])
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["data"]), 5)
        self.assertEqual(body["data"][0]["status_code"], 200)

    def test_history_respects_limit(self) -> None:
        response = APIClient().get(
            reverse("station-health-history", args=["bbc_1xtra"]),
            {"limit": 2},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["data"]), 2)

    def test_history_invalid_limit(self) -> None:
        response = APIClient().get(
            reverse("station-health-history", args=["bbc_1xtra"]),
            {"limit": "abc"},
        )
        self.assertEqual(response.status_code, 400)

    def test_history_limit_out_of_range(self) -> None:
        response = APIClient().get(
            reverse("station-health-history", args=["bbc_1xtra"]),
            {"limit": 999},
        )
        self.assertEqual(response.status_code, 400)

    def test_history_unknown_station_404(self) -> None:
        response = APIClient().get(
            reverse("station-health-history", args=["does_not_exist"])
        )
        self.assertEqual(response.status_code, 404)


class TimedDecoratorTestCase(TestCase):
    """Coverage for the ``@timed`` decorator wiring on a real view."""

    def setUp(self) -> None:
        cache.clear()
        self.provider = _make_provider()
        _make_station(self.provider, "bbc_1xtra")

    def test_list_view_records_latency(self) -> None:
        before = radio_api_request_latency_seconds.labels(
            endpoint="stations.list", method="GET"
        )._sum.get()  # type: ignore[attr-defined]
        response = APIClient().get(reverse("station-list"))
        self.assertEqual(response.status_code, 200)
        after = radio_api_request_latency_seconds.labels(
            endpoint="stations.list", method="GET"
        )._sum.get()  # type: ignore[attr-defined]
        self.assertGreater(after, before)
