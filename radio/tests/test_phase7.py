"""Tests for Phase 7 — fallback stations, analytics, and now-playing."""

from __future__ import annotations

import secrets
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from radio.models import (
    ListeningHistory,
    NowPlaying,
    Provider,
    Station,
    StationAnalytics,
)
from radio.services import (
    DEFAULT_FALLBACK_STATION_MAP,
    _parse_icy_stream_title,
    fetch_icy_metadata,
    get_fallback_station,
    get_fallback_station_map,
    get_now_playing,
    get_station_analytics,
    refresh_now_playing,
    rollup_station_analytics,
)

User = get_user_model()


def _provider(slug: str = "bbc") -> Provider:
    return Provider.objects.create(
        slug=slug, name=slug.upper(), is_active=True
    )


def _station(
    provider: Provider,
    id_: str,
    *,
    metadata_url: str = "",
    is_available: bool | None = None,
) -> Station:
    s = Station.objects.create(
        id=id_,
        name=id_,
        provider=provider,
        country="UK",
        language="English",
        stream_url=f"https://example.test/{id_}",
        metadata_url=metadata_url,
        is_active=True,
    )
    if is_available is not None:
        s.is_available = is_available
        s.save(update_fields=["is_available", "updated_at"])
    return s


class FallbackStationTestCase(TestCase):
    """Service-level coverage for the fallback-station map."""

    def setUp(self) -> None:
        self.provider = _provider()
        self.primary = _station(self.provider, "bbc_1xtra", is_available=False)
        self.fallback = _station(
            self.provider, "bbc_radio1", is_available=True
        )

    def test_default_map_includes_1xtra(self) -> None:
        self.assertEqual(
            DEFAULT_FALLBACK_STATION_MAP.get("bbc_1xtra"), "bbc_radio1"
        )

    def test_get_fallback_station_map_returns_defaults(self) -> None:
        mapping = get_fallback_station_map()
        self.assertEqual(mapping.get("bbc_1xtra"), "bbc_radio1")

    def test_settings_override_wins(self) -> None:
        with self.settings(
            RADIO_FALLBACK_STATION_MAP={"bbc_1xtra": "bbc_radio2"}
        ):
            mapping = get_fallback_station_map()
        self.assertEqual(mapping.get("bbc_1xtra"), "bbc_radio2")
        # With the new semantics, an explicit setting replaces the
        # defaults entirely. Only the override is in the map.
        self.assertNotIn("bbc_radio1", mapping)

    def test_get_fallback_station_returns_active_fallback(self) -> None:
        result = get_fallback_station("bbc_1xtra")
        self.assertIsNotNone(result)
        self.assertEqual(result.id, "bbc_radio1")

    def test_get_fallback_station_inactive_returns_none(self) -> None:
        self.fallback.is_active = False
        self.fallback.save()
        self.assertIsNone(get_fallback_station("bbc_1xtra"))

    def test_get_fallback_station_unknown_primary_returns_none(self) -> None:
        self.assertIsNone(get_fallback_station("not_in_map"))

    def test_get_fallback_station_self_loop_returns_none(self) -> None:
        with self.settings(
            RADIO_FALLBACK_STATION_MAP={"bbc_radio1": "bbc_radio1"}
        ):
            self.assertIsNone(get_fallback_station("bbc_radio1"))


class StreamViewFallbackTestCase(TestCase):
    """Endpoint-level coverage for the fallback in the 503 payload."""

    def setUp(self) -> None:
        self.provider = _provider()
        self.primary = _station(self.provider, "bbc_1xtra", is_available=False)
        self.fallback = _station(
            self.provider, "bbc_radio1", is_available=True
        )

    def test_503_includes_fallback_payload(self) -> None:
        response = APIClient().get(
            reverse("station-stream", args=["bbc_1xtra"])
        )
        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(body["status"], 1)
        fallback = body["errors"]["fallback"]
        self.assertIsNotNone(fallback)
        self.assertEqual(fallback["station_id"], "bbc_radio1")
        self.assertEqual(
            fallback["stream_url"], "https://example.test/bbc_radio1"
        )

    def test_503_no_fallback_when_unmapped(self) -> None:
        with self.settings(RADIO_FALLBACK_STATION_MAP={}):
            response = APIClient().get(
                reverse("station-stream", args=["bbc_1xtra"])
            )
        self.assertEqual(response.status_code, 503)
        self.assertIsNone(response.json()["errors"]["fallback"])


class IcyMetadataParseTestCase(TestCase):
    """Unit coverage for the ICY StreamTitle parser."""

    def test_parses_simple_title(self) -> None:
        self.assertEqual(
            _parse_icy_stream_title("StreamTitle='Hello - World';"),
            "Hello - World",
        )

    def test_returns_blank_when_missing(self) -> None:
        self.assertEqual(_parse_icy_stream_title("foo=bar"), "")

    def test_handles_unterminated_string(self) -> None:
        # If the single-quote is never closed, we take everything
        # up to the next semicolon.
        self.assertEqual(
            _parse_icy_stream_title("StreamTitle='Foo - Bar; other=1"),
            "Foo - Bar",
        )


class IcyMetadataFetchTestCase(TestCase):
    """Service-level coverage for ``fetch_icy_metadata``."""

    def test_returns_empty_on_http_error(self) -> None:
        with patch("radio.services.httpx.Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.get.return_value.status_code = 404
            self.assertEqual(fetch_icy_metadata("https://example.test/s"), {})

    def test_returns_empty_on_httpx_error(self) -> None:
        import httpx as _httpx

        with patch("radio.services.httpx.Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.get.side_effect = _httpx.ConnectError("boom")
            self.assertEqual(fetch_icy_metadata("https://example.test/s"), {})

    def test_parses_payload(self) -> None:
        fake_body = (
            b"StreamTitle='Artist Name - Track Title';StreamUrl='http://x';"
        )
        with patch("radio.services.httpx.Client") as client_cls:
            client = client_cls.return_value.__enter__.return_value
            client.get.return_value.status_code = 200
            client.get.return_value.content = fake_body
            result = fetch_icy_metadata("https://example.test/s")
        self.assertEqual(result["artist"], "Artist Name")
        self.assertEqual(result["track_title"], "Track Title")


class RefreshNowPlayingTestCase(TestCase):
    """Coverage for the periodic refresh entry point."""

    def setUp(self) -> None:
        self.provider = _provider()
        self.station = _station(
            self.provider,
            "bbc_1xtra",
            metadata_url="https://example.test/meta",
        )

    def test_refresh_creates_now_playing_row(self) -> None:
        with patch(
            "radio.services.fetch_icy_metadata",
            return_value={"artist": "A", "track_title": "T"},
        ):
            summary = refresh_now_playing()
        self.assertEqual(summary["updated"], 1)
        row = NowPlaying.objects.get(station=self.station)
        self.assertEqual(row.artist, "A")
        self.assertEqual(row.track_title, "T")

    def test_refresh_skips_when_no_metadata_url(self) -> None:
        # Stations with no ``metadata_url`` are filtered out at the
        # query layer, so ``refresh_now_playing`` never even
        # attempts the HTTP call. Strip the metadata URL from
        # the setUp station and add a fresh station that also
        # has none configured; both should be filtered, no
        # fetches should happen.
        Station.objects.filter(pk=self.station.pk).update(metadata_url="")
        _station(self.provider, "no_meta", metadata_url="")
        with patch("radio.services.fetch_icy_metadata") as mocked:
            summary = refresh_now_playing()
        self.assertEqual(summary["attempted"], 0)
        self.assertEqual(summary["updated"], 0)
        self.assertEqual(summary["skipped"], 0)
        mocked.assert_not_called()

    def test_refresh_handles_missing_data(self) -> None:
        with patch("radio.services.fetch_icy_metadata", return_value={}):
            summary = refresh_now_playing()
        self.assertEqual(summary["updated"], 0)
        self.assertEqual(summary["skipped"], 1)

    def test_get_now_playing_returns_none_when_absent(self) -> None:
        self.assertIsNone(get_now_playing("bbc_1xtra"))


class NowPlayingEndpointTestCase(TestCase):
    """Endpoint coverage for ``/stations/<id>/now-playing/``."""

    def setUp(self) -> None:
        self.provider = _provider()
        self.station = _station(self.provider, "bbc_1xtra")

    def test_returns_null_when_no_row(self) -> None:
        response = APIClient().get(
            reverse("station-now-playing", args=["bbc_1xtra"])
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["data"])

    def test_returns_row(self) -> None:
        NowPlaying.objects.create(
            station=self.station,
            artist="A",
            track_title="T",
        )
        response = APIClient().get(
            reverse("station-now-playing", args=["bbc_1xtra"])
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["data"]["artist"], "A")
        self.assertEqual(body["data"]["track_title"], "T")

    def test_unknown_station_404(self) -> None:
        response = APIClient().get(
            reverse("station-now-playing", args=["nope"])
        )
        self.assertEqual(response.status_code, 404)


class StationAnalyticsRollupTestCase(TestCase):
    """Coverage for the analytics rollup and endpoint."""

    def setUp(self) -> None:
        self.provider = _provider()
        self.station_a = _station(self.provider, "bbc_1xtra")
        self.station_b = _station(self.provider, "bbc_radio1")
        self.user = User.objects.create_user(
            username="dana", password=secrets.token_urlsafe(12)
        )
        self.other_user = User.objects.create_user(
            username="erin", password=secrets.token_urlsafe(12)
        )

    def _add_history(
        self, station: Station, user: object, days_ago: int
    ) -> None:
        # ``started_at`` has ``auto_now_add=True``; pass a sentinel on
        # create and override via ``.update()`` so the backdating
        # actually takes effect.
        row = ListeningHistory.objects.create(user=user, station=station)
        ListeningHistory.objects.filter(pk=row.pk).update(
            started_at=timezone.now() - timedelta(days=days_ago)
        )

    def test_rollup_writes_one_row_per_station(self) -> None:
        self._add_history(self.station_a, self.user, 1)
        self._add_history(self.station_a, self.user, 1)
        self._add_history(self.station_a, self.other_user, 1)
        self._add_history(self.station_b, self.user, 1)
        summary = rollup_station_analytics(lookback_days=2)
        self.assertEqual(summary["rows_written"], 2)
        rows = list(StationAnalytics.objects.filter(station=self.station_a))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].total_listens, 3)
        self.assertEqual(rows[0].unique_users, 2)

    def test_rollup_is_idempotent(self) -> None:
        self._add_history(self.station_a, self.user, 1)
        rollup_station_analytics(lookback_days=2)
        rollup_station_analytics(lookback_days=2)
        self.assertEqual(
            StationAnalytics.objects.filter(station=self.station_a).count(),
            1,
        )

    def test_rollup_respects_lookback_window(self) -> None:
        # ``days_ago=0`` lands in today's bucket; ``days_ago=5``
        # falls outside a 1-day window.
        self._add_history(self.station_a, self.user, 0)
        self._add_history(self.station_a, self.user, 5)
        rollup_station_analytics(lookback_days=1)
        rows = list(StationAnalytics.objects.filter(station=self.station_a))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].total_listens, 1)
        # Expanding the window to 7 days pulls the older row in
        # under its own day bucket.
        rollup_station_analytics(lookback_days=7)
        rows = list(StationAnalytics.objects.filter(station=self.station_a))
        self.assertEqual(len(rows), 2)
        rows_by_date = {r.date: r for r in rows}
        self.assertEqual(rows_by_date[timezone.now().date()].total_listens, 1)
        self.assertEqual(
            rows_by_date[
                timezone.now().date() - timedelta(days=5)
            ].total_listens,
            1,
        )

    def test_endpoint_returns_rows(self) -> None:
        for i in range(3):
            StationAnalytics.objects.create(
                station=self.station_a,
                date=timezone.now().date() - timedelta(days=i),
                total_listens=10 + i,
                unique_users=2,
            )
        response = APIClient().get(
            reverse("station-analytics", args=["bbc_1xtra"])
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["data"]), 3)

    def test_endpoint_respects_days(self) -> None:
        for i in range(5):
            StationAnalytics.objects.create(
                station=self.station_a,
                date=timezone.now().date() - timedelta(days=i),
                total_listens=1,
                unique_users=1,
            )
        response = APIClient().get(
            reverse("station-analytics", args=["bbc_1xtra"]),
            {"days": 2},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["data"]), 2)

    def test_endpoint_invalid_days(self) -> None:
        response = APIClient().get(
            reverse("station-analytics", args=["bbc_1xtra"]),
            {"days": "abc"},
        )
        self.assertEqual(response.status_code, 400)

    def test_endpoint_days_out_of_range(self) -> None:
        response = APIClient().get(
            reverse("station-analytics", args=["bbc_1xtra"]),
            {"days": 999},
        )
        self.assertEqual(response.status_code, 400)

    def test_endpoint_unknown_station_404(self) -> None:
        response = APIClient().get(reverse("station-analytics", args=["nope"]))
        self.assertEqual(response.status_code, 404)

    def test_get_station_analytics_clamps_days(self) -> None:
        for i in range(3):
            StationAnalytics.objects.create(
                station=self.station_a,
                date=timezone.now().date() - timedelta(days=i),
                total_listens=1,
                unique_users=1,
            )
        # days > 90 should be clamped.
        rows = get_station_analytics("bbc_1xtra", days=200)
        self.assertEqual(len(rows), 3)
        # days < 1 should be clamped to 1.
        rows = get_station_analytics("bbc_1xtra", days=0)
        self.assertEqual(len(rows), 1)
