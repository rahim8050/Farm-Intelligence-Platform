from __future__ import annotations

# ruff: noqa: S101
import asyncio
import secrets
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import cast
from zoneinfo import ZoneInfo

import httpx
import pytest
from django.conf import LazySettings
from django.core.cache import caches
from django.utils import timezone as dj_timezone
from rest_framework.exceptions import ValidationError

from weather.engines.base import WeatherProvider
from weather.engines.nasa_power import (
    NasaPowerProvider,
    NasaPowerUpstreamError,
)
from weather.engines.open_meteo import OpenMeteoProvider
from weather.engines.registry import validate_provider
from weather.engines.types import (
    CurrentWeather,
    DailyForecast,
    Location,
    ProviderName,
    WeeklyReport,
)
from weather.metrics import (
    weather_cache_hits_total,
    weather_cache_misses_total,
    weather_provider_errors_total,
    weather_provider_requests_total,
)
from weather.serializers import (
    MAX_RANGE_DAYS,
    BaseWeatherParamsSerializer,
    RangeWeatherParamsSerializer,
    serialize_current,
    serialize_daily,
    serialize_weekly,
)
from weather.services import (
    DEFAULT_TZ,
    PROVIDER_REGISTRY,
    CacheKey,
    FarmCacheKey,
    _aggregate_weekly,
    _fetch_daily_forecasts,
    _handle_upstream_error,
    _lock_cache_key,
    _resolve_farm_location,
    _select_provider,
    _stale_cache_key,
    _wait_for_cached_value,
    get_current_weather,
    get_daily_forecast,
    get_farm_current_weather,
    get_farm_daily_summary,
    get_farm_hourly_forecast,
    get_weekly_report,
)


def _clear_cache() -> None:
    caches["default"].clear()


def test_open_meteo_current_parses_observed_timezone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_cache()
    payload: dict[str, object] = {
        "current": {
            "time": "2025-01-02T10:00",
            "temperature_2m": 24.2,
            "wind_speed_10m": 3.5,
        }
    }

    async def fake_request(
        self: OpenMeteoProvider, params: dict[str, object]
    ) -> dict[str, object]:
        return payload

    monkeypatch.setattr(OpenMeteoProvider, "_request", fake_request)
    result = asyncio.run(
        get_current_weather(
            lat=1.0,
            lon=36.0,
            tz="Africa/Nairobi",
            provider="open_meteo",
        )
    )
    serialized = serialize_current(result)
    assert serialized["temperature_c"] == pytest.approx(24.2)
    assert serialized["wind_speed_mps"] == pytest.approx(3.5)
    assert str(serialized["observed_at"]).endswith("+03:00")


def test_nasa_power_daily_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_cache()
    payload: dict[str, object] = {
        "properties": {
            "parameter": {
                "T2M_MIN": {"20250101": 20.0},
                "T2M_MAX": {"20250101": 30.0},
                "PRECTOTCORR": {"20250101": -999, "20250102": 5.0},
            },
            "fill_value": -999,
        }
    }

    async def fake_request(
        self: NasaPowerProvider, params: dict[str, object]
    ) -> dict[str, object]:
        return payload

    monkeypatch.setattr(NasaPowerProvider, "_request", fake_request)
    provider = NasaPowerProvider()
    forecasts = asyncio.run(
        provider.daily(
            Location(lat=1.0, lon=36.0, tz="Africa/Nairobi"),
            date(2025, 1, 1),
            date(2025, 1, 2),
        )
    )
    assert len(forecasts) == 2
    first = forecasts[0]
    assert first.day == date(2025, 1, 1)
    assert first.t_min_c == pytest.approx(20.0)
    assert first.t_max_c == pytest.approx(30.0)
    assert first.precipitation_mm is None
    second = forecasts[1]
    assert second.precipitation_mm == pytest.approx(5.0)


def test_nasa_power_daily_request_params_use_local_dates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_request(
        self: NasaPowerProvider, params: dict[str, object]
    ) -> dict[str, object]:
        captured.update(params)
        return {
            "properties": {"parameter": {}, "fill_value": -999},
        }

    monkeypatch.setattr(NasaPowerProvider, "_request", fake_request)
    provider = NasaPowerProvider()
    asyncio.run(
        provider.daily(
            Location(lat=1.0, lon=36.0, tz="Africa/Nairobi"),
            date(2026, 1, 8),
            date(2026, 1, 8),
        )
    )
    assert captured["start"] == "20260108"
    assert captured["end"] == "20260108"
    assert captured["community"] == "AG"
    assert captured["time-standard"] == "UTC"


def test_open_meteo_daily_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_cache()
    payload: dict[str, object] = {
        "daily": {
            "time": ["2025-02-01", "invalid"],
            "temperature_2m_min": [12.0, 13.0],
            "temperature_2m_max": [22.0, None],
            "precipitation_sum": [0.5, 1.0],
        }
    }

    async def fake_request(
        self: OpenMeteoProvider, params: dict[str, object]
    ) -> dict[str, object]:
        return payload

    monkeypatch.setattr(OpenMeteoProvider, "_request", fake_request)
    provider = OpenMeteoProvider()
    forecasts = asyncio.run(
        provider.daily(
            Location(lat=1.0, lon=36.0, tz="Africa/Nairobi"),
            date(2025, 2, 1),
            date(2025, 2, 2),
        )
    )
    assert len(forecasts) == 1
    forecast = forecasts[0]
    assert forecast.day == date(2025, 2, 1)
    assert forecast.t_min_c == pytest.approx(12.0)
    assert forecast.t_max_c == pytest.approx(22.0)
    assert forecast.precipitation_mm == pytest.approx(0.5)
    assert forecast.source == "open_meteo"


def test_open_meteo_daily_precipitation_maps_to_serialized_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_cache()
    payload: dict[str, object] = {
        "daily": {
            "time": ["2025-06-01"],
            "temperature_2m_min": [10.0],
            "temperature_2m_max": [20.0],
            "precipitation_sum": [2.5],
        }
    }

    class FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> object:
            return payload

    class FakeAsyncClient:
        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get(self, *_: object, **__: object) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_: FakeAsyncClient())
    provider = OpenMeteoProvider(max_retries=0)
    forecasts = asyncio.run(
        provider.daily(
            Location(lat=1.0, lon=36.0, tz="Africa/Nairobi"),
            date(2025, 6, 1),
            date(2025, 6, 1),
        )
    )
    serialized = serialize_daily(forecasts)
    assert serialized[0]["precipitation_mm"] == pytest.approx(2.5)


def test_provider_switching_default_and_override(
    monkeypatch: pytest.MonkeyPatch, settings: LazySettings
) -> None:
    _clear_cache()
    settings.WEATHER_PROVIDER_DEFAULT = "open_meteo"
    open_payload: dict[str, object] = {
        "current": {
            "time": "2025-02-01T08:00",
            "temperature_2m": 22.0,
            "wind_speed_10m": 4.0,
        }
    }
    nasa_payload: dict[str, object] = {
        "properties": {
            "parameter": {
                "T2M_MIN": {"20250201": 18.0},
                "T2M_MAX": {"20250201": 28.0},
                "PRECTOTCORR": {"20250201": 2.0},
            },
            "fill_value": -999,
        }
    }

    async def fake_open(
        self: OpenMeteoProvider, params: dict[str, object]
    ) -> dict[str, object]:
        return open_payload

    async def fake_nasa(
        self: NasaPowerProvider, params: dict[str, object]
    ) -> dict[str, object]:
        return nasa_payload

    monkeypatch.setattr(OpenMeteoProvider, "_request", fake_open)
    monkeypatch.setattr(NasaPowerProvider, "_request", fake_nasa)
    monkeypatch.setattr(
        dj_timezone,
        "now",
        lambda: datetime(
            2025,
            2,
            1,
            tzinfo=timezone.utc,  # noqa: UP017
        ),
    )

    default_result = asyncio.run(
        get_current_weather(lat=0.5, lon=36.8, tz=DEFAULT_TZ)
    )
    assert default_result.source == "open_meteo"

    nasa_result = asyncio.run(
        get_current_weather(
            lat=0.5,
            lon=36.8,
            tz=DEFAULT_TZ,
            provider="nasa_power",
        )
    )
    assert nasa_result.source == "nasa_power"
    assert nasa_result.temperature_c == pytest.approx(23.0)


def test_weekly_bucketing_monday_to_sunday() -> None:
    forecasts = [
        DailyForecast(
            day=date(2025, 1, 6),
            t_min_c=10.0,
            t_max_c=20.0,
            precipitation_mm=1.0,
            source="open_meteo",
        ),
        DailyForecast(
            day=date(2025, 1, 7),
            t_min_c=None,
            t_max_c=22.0,
            precipitation_mm=2.0,
            source="open_meteo",
        ),
        DailyForecast(
            day=date(2025, 1, 12),
            t_min_c=12.0,
            t_max_c=None,
            precipitation_mm=0.5,
            source="open_meteo",
        ),
        DailyForecast(
            day=date(2025, 1, 13),
            t_min_c=9.0,
            t_max_c=19.0,
            precipitation_mm=0.0,
            source="open_meteo",
        ),
    ]

    reports = _aggregate_weekly(forecasts, "open_meteo")
    assert len(reports) == 2
    first = reports[0]
    assert first.week_start == date(2025, 1, 6)
    assert first.week_end == date(2025, 1, 12)
    assert first.t_min_avg_c == pytest.approx((10.0 + 12.0) / 2)
    assert first.t_max_avg_c == pytest.approx((20.0 + 22.0) / 2)
    assert first.precipitation_sum_mm == pytest.approx(3.5)
    second = reports[1]
    assert second.week_start == date(2025, 1, 13)
    assert second.precipitation_sum_mm == pytest.approx(0.0)


def test_cache_hits_and_misses_increment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_cache()

    async def fake_daily(
        self: OpenMeteoProvider, loc: Location, start: date, end: date
    ) -> list[DailyForecast]:
        return [
            DailyForecast(
                day=start,
                t_min_c=15.0,
                t_max_c=25.0,
                precipitation_mm=1.0,
                source="open_meteo",
            )
        ]

    monkeypatch.setattr(OpenMeteoProvider, "daily", fake_daily)
    miss_counter = weather_cache_misses_total.labels(
        provider="open_meteo", endpoint="daily"
    )
    hit_counter = weather_cache_hits_total.labels(
        provider="open_meteo", endpoint="daily"
    )
    request_counter = weather_provider_requests_total.labels(
        provider="open_meteo", endpoint="daily"
    )
    misses_before = miss_counter._value.get()
    hits_before = hit_counter._value.get()
    requests_before = request_counter._value.get()

    first = asyncio.run(
        get_daily_forecast(
            lat=1.1,
            lon=36.9,
            start=date(2025, 3, 1),
            end=date(2025, 3, 1),
            tz=DEFAULT_TZ,
        )
    )
    second = asyncio.run(
        get_daily_forecast(
            lat=1.1,
            lon=36.9,
            start=date(2025, 3, 1),
            end=date(2025, 3, 1),
            tz=DEFAULT_TZ,
        )
    )
    assert first == second

    misses_after = miss_counter._value.get()
    hits_after = hit_counter._value.get()
    requests_after = request_counter._value.get()
    assert misses_after == misses_before + 1
    assert hits_after == hits_before + 1
    assert requests_after == requests_before + 1


def test_error_metrics_increment(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_cache()
    error = httpx.HTTPStatusError(
        "boom",
        request=httpx.Request("GET", "https://example.com"),
        response=httpx.Response(503),
    )

    async def failing_daily(*_: object, **__: object) -> None:
        raise error

    monkeypatch.setattr(OpenMeteoProvider, "daily", failing_daily)
    error_counter = weather_provider_errors_total.labels(
        provider="open_meteo", endpoint="daily", error_type="HTTPStatusError"
    )
    before = error_counter._value.get()
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(
            get_daily_forecast(
                lat=2.0,
                lon=37.1,
                start=date(2025, 4, 1),
                end=date(2025, 4, 1),
                tz=DEFAULT_TZ,
            )
        )
    after = error_counter._value.get()
    assert after == before + 1


def test_base_weather_params_timezone_and_provider_validation() -> None:
    serializer = BaseWeatherParamsSerializer(
        data={"lat": 1.0, "lon": 36.0, "tz": "Invalid/Zone"}
    )
    assert not serializer.is_valid()
    assert "Invalid timezone." in serializer.errors["tz"][0]

    serializer = BaseWeatherParamsSerializer(
        data={"lat": 1.0, "lon": 36.0, "provider": "OPEN_METEO"}
    )
    assert serializer.is_valid()
    assert serializer.validated_data["provider"] == "open_meteo"

    serializer = BaseWeatherParamsSerializer(
        data={"lat": 1.0, "lon": 36.0, "provider": "unknown"}
    )
    assert not serializer.is_valid()
    assert "Unknown provider." in serializer.errors["provider"][0]

    base = BaseWeatherParamsSerializer()
    assert base.validate_provider(None) is None
    assert base.validate_provider("") is None


def test_range_weather_params_validation() -> None:
    serializer = RangeWeatherParamsSerializer(
        data={
            "lat": 1.0,
            "lon": 36.0,
            "start": "2025-02-10",
            "end": "2025-02-01",
        }
    )
    assert not serializer.is_valid()
    assert (
        "start must be on or before end."
        in serializer.errors["non_field_errors"][0]
    )

    start = date(2020, 1, 1)
    end = start + timedelta(days=MAX_RANGE_DAYS + 1)
    serializer = RangeWeatherParamsSerializer(
        data={
            "lat": 1.0,
            "lon": 36.0,
            "start": start.isoformat(),
            "end": end.isoformat(),
        }
    )
    assert not serializer.is_valid()
    assert "WEATHER_MAX_RANGE_DAYS" in serializer.errors["non_field_errors"][0]


def test_serialization_helpers() -> None:
    observed = datetime(2025, 1, 1, 8, 0)
    current = CurrentWeather(
        observed_at=observed,
        temperature_c=20.0,
        wind_speed_mps=3.0,
        source="open_meteo",
    )
    current_data = serialize_current(current)
    assert str(current_data["observed_at"]).endswith("+00:00")

    daily = [
        DailyForecast(
            day=date(2025, 1, 1),
            t_min_c=10.0,
            t_max_c=20.0,
            precipitation_mm=None,
            source="open_meteo",
        )
    ]
    daily_data = serialize_daily(daily)
    assert daily_data[0]["day"] == date(2025, 1, 1).isoformat()

    weekly = [
        WeeklyReport(
            week_start=date(2025, 1, 1),
            week_end=date(2025, 1, 7),
            t_min_avg_c=None,
            t_max_avg_c=None,
            precipitation_sum_mm=None,
            days=daily,
            source="open_meteo",
        )
    ]
    weekly_data = serialize_weekly(weekly)
    assert weekly_data[0]["week_start"] == date(2025, 1, 1).isoformat()


def test_partial_flags_for_complete_days(
    monkeypatch: pytest.MonkeyPatch,
    settings: LazySettings,
) -> None:
    settings.NASA_POWER_DAILY_LAG_DAYS = 2
    monkeypatch.setattr(dj_timezone, "localdate", lambda: date(2025, 1, 10))
    forecasts = [
        DailyForecast(
            day=date(2025, 1, 6),
            t_min_c=10.0,
            t_max_c=20.0,
            precipitation_mm=1.0,
            source="nasa_power",
        ),
        DailyForecast(
            day=date(2025, 1, 7),
            t_min_c=12.0,
            t_max_c=22.0,
            precipitation_mm=0.5,
            source="nasa_power",
        ),
    ]

    daily_data = serialize_daily(forecasts)
    assert daily_data[0]["is_partial"] is False
    assert daily_data[0]["missing_fields"] == []
    assert daily_data[1]["is_partial"] is False
    assert daily_data[1]["missing_fields"] == []

    weekly_reports = _aggregate_weekly(forecasts, "nasa_power")
    weekly_data = serialize_weekly(weekly_reports)
    report = weekly_data[0]
    assert report["is_partial"] is False
    assert report["missing_days_count"] == 0
    assert report["t_min_avg_c"] == pytest.approx((10.0 + 12.0) / 2)
    assert report["t_max_avg_c"] == pytest.approx((20.0 + 22.0) / 2)
    assert report["precipitation_sum_mm"] == pytest.approx(1.5)


def test_partial_flags_for_recent_nasa_nulls(
    monkeypatch: pytest.MonkeyPatch,
    settings: LazySettings,
) -> None:
    settings.NASA_POWER_DAILY_LAG_DAYS = 2
    today = date(2025, 1, 10)
    monkeypatch.setattr(dj_timezone, "localdate", lambda: today)
    forecasts = [
        DailyForecast(
            day=today - timedelta(days=1),
            t_min_c=11.0,
            t_max_c=21.0,
            precipitation_mm=0.8,
            source="nasa_power",
        ),
        DailyForecast(
            day=today,
            t_min_c=None,
            t_max_c=None,
            precipitation_mm=None,
            source="nasa_power",
        ),
    ]

    daily_data = serialize_daily(forecasts)
    today_payload = next(
        entry for entry in daily_data if entry["day"] == today.isoformat()
    )
    assert today_payload["is_partial"] is True
    missing_fields = cast(list[str], today_payload["missing_fields"])
    assert set(missing_fields) == {
        "t_min_c",
        "t_max_c",
        "precipitation_mm",
    }

    weekly_reports = _aggregate_weekly(forecasts, "nasa_power")
    weekly_data = serialize_weekly(weekly_reports)
    report = weekly_data[0]
    assert report["is_partial"] is True
    assert report["missing_days_count"] == 1
    assert report["t_min_avg_c"] == pytest.approx(11.0)
    assert report["t_max_avg_c"] == pytest.approx(21.0)
    assert report["precipitation_sum_mm"] == pytest.approx(0.8)


def test_open_meteo_current_fallbacks_to_now(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_cache()
    fixed_now = datetime(2025, 5, 1, tzinfo=UTC)

    async def fake_request(
        self: OpenMeteoProvider, params: dict[str, object]
    ) -> dict[str, object]:
        return {"current": {"temperature_2m": 21.0, "wind_speed_10m": 2.5}}

    monkeypatch.setattr(OpenMeteoProvider, "_request", fake_request)
    monkeypatch.setattr(
        "weather.engines.open_meteo.timezone.now", lambda: fixed_now
    )
    provider = OpenMeteoProvider()
    result = asyncio.run(
        provider.current(Location(lat=1.0, lon=36.0, tz="UTC"))
    )
    assert result.observed_at == fixed_now


def test_open_meteo_parse_helpers() -> None:
    provider = OpenMeteoProvider()
    zone = ZoneInfo("UTC")
    assert provider._parse_datetime(None, zone) is None
    assert provider._parse_datetime("bad", zone) is None
    parsed = provider._parse_datetime("2025-01-01T00:00Z", zone)
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert provider._parse_date(None) is None
    assert provider._parse_date("bad") is None
    assert provider._list_value([1.0], 3) is None
    assert provider._to_float(None) is None
    assert provider._to_float("nope") is None


def test_open_meteo_request_retries_on_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_cache()

    class FakeResponse:
        def __init__(self, status_code: int, payload: object) -> None:
            self.status_code = status_code
            self._payload = payload

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                request = httpx.Request("GET", "https://example.com")
                response = httpx.Response(self.status_code)
                raise httpx.HTTPStatusError(
                    "boom", request=request, response=response
                )

        def json(self) -> object:
            return self._payload

    response_iter = iter(
        [
            FakeResponse(502, {"error": "bad"}),
            FakeResponse(200, {"ok": True}),
        ]
    )

    class FakeAsyncClient:
        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get(self, *_: object, **__: object) -> FakeResponse:
            return next(response_iter)

    async def fake_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_: FakeAsyncClient())
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    provider = OpenMeteoProvider(max_retries=1, backoff_seconds=0.0)
    payload = asyncio.run(provider._request({"lat": 1.0}))
    assert payload == {"ok": True}


def test_open_meteo_request_invalid_payload_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> object:
            return ["bad"]

    class FakeAsyncClient:
        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get(self, *_: object, **__: object) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_: FakeAsyncClient())
    provider = OpenMeteoProvider(max_retries=0)
    with pytest.raises(ValueError, match="Unexpected Open-Meteo"):
        asyncio.run(provider._request({"lat": 1.0}))


def test_nasa_power_daily_skips_invalid_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload: dict[str, object] = {
        "properties": {
            "parameter": {
                "T2M_MIN": ["bad"],
                "T2M_MAX": {"bad": 10.0, "20250101": 21.0},
                "PRECTOTCORR": {"20250101": 5.0},
            },
            "fill_value": -999,
        }
    }

    async def fake_request(
        self: NasaPowerProvider, params: dict[str, object]
    ) -> dict[str, object]:
        return payload

    monkeypatch.setattr(NasaPowerProvider, "_request", fake_request)
    provider = NasaPowerProvider()
    forecasts = asyncio.run(
        provider.daily(
            Location(lat=1.0, lon=36.0, tz="UTC"),
            date(2025, 1, 1),
            date(2025, 1, 2),
        )
    )
    assert len(forecasts) == 1
    assert forecasts[0].day == date(2025, 1, 1)
    assert forecasts[0].t_min_c is None


def test_nasa_power_request_invalid_payload_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> object:
            return ["bad"]

    class FakeAsyncClient:
        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get(self, *_: object, **__: object) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_: FakeAsyncClient())
    provider = NasaPowerProvider()
    with pytest.raises(ValueError, match="Unexpected NASA POWER"):
        asyncio.run(provider._request({"lat": 1.0}))


def test_nasa_power_request_http_error_maps_to_upstream_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = httpx.Response(
        status_code=422,
        request=httpx.Request("GET", "https://example.com"),
        text="bad request",
    )

    class FakeAsyncClient:
        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get(self, *_: object, **__: object) -> httpx.Response:
            return response

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_: FakeAsyncClient())
    provider = NasaPowerProvider()
    with pytest.raises(NasaPowerUpstreamError) as exc_info:
        asyncio.run(provider._request({"lat": 1.0}))
    assert exc_info.value.status_code == 502


def test_nasa_power_helpers() -> None:
    provider = NasaPowerProvider()
    zone = ZoneInfo("UTC")
    assert provider._parse_day_to_local("bad", zone) is None

    assert provider._extract_value([], "20250101", -999) is None
    assert (
        provider._extract_value({"20250101": None}, "20250101", -999) is None
    )
    assert (
        provider._extract_value({"20250101": -999}, "20250101", -999) is None
    )
    assert provider._extract_value({"20250101": "x"}, "20250101", -999) is None

    class FlakyFloat:
        def __init__(self) -> None:
            self.calls = 0

        def __float__(self) -> float:
            self.calls += 1
            if self.calls == 1:
                return 1.0
            raise ValueError("boom")

    assert (
        provider._extract_value({"20250101": FlakyFloat()}, "20250101", -999)
        is None
    )

    assert provider._choose_temperature(None) is None
    assert (
        provider._choose_temperature(
            DailyForecast(
                day=date(2025, 1, 1),
                t_min_c=10.0,
                t_max_c=20.0,
                precipitation_mm=None,
                source="nasa_power",
            )
        )
        == 15.0
    )
    assert (
        provider._choose_temperature(
            DailyForecast(
                day=date(2025, 1, 1),
                t_min_c=None,
                t_max_c=20.0,
                precipitation_mm=None,
                source="nasa_power",
            )
        )
        == 20.0
    )
    assert (
        provider._choose_temperature(
            DailyForecast(
                day=date(2025, 1, 1),
                t_min_c=5.0,
                t_max_c=None,
                precipitation_mm=None,
                source="nasa_power",
            )
        )
        == 5.0
    )


def test_registry_validation_rejects_unknown_provider() -> None:
    registry = cast(
        dict[ProviderName, WeatherProvider],
        {"open_meteo": OpenMeteoProvider()},
    )
    with pytest.raises(ValueError, match="Unsupported weather provider"):
        validate_provider("nope", registry)


def test_select_provider_invalid_raises_validation_error() -> None:
    with pytest.raises(ValidationError):
        _select_provider("nope")


def test_get_current_weather_cache_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_cache()
    provider = PROVIDER_REGISTRY["open_meteo"]
    weather = CurrentWeather(
        observed_at=datetime(2025, 1, 1, tzinfo=UTC),
        temperature_c=20.0,
        wind_speed_mps=3.0,
        source="open_meteo",
    )
    key = CacheKey(
        endpoint="current",
        provider="open_meteo",
        lat=1.0,
        lon=2.0,
        tz=DEFAULT_TZ,
    )
    caches["default"].set(key.as_string(), weather, 60)
    monkeypatch.setattr(provider, "current", lambda *_: None)
    result = asyncio.run(get_current_weather(lat=1.0, lon=2.0, tz=DEFAULT_TZ))
    assert result == weather


def test_get_current_weather_error_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_cache()

    async def failing_current(*_: object, **__: object) -> None:
        raise RuntimeError("boom")

    provider = PROVIDER_REGISTRY["open_meteo"]
    monkeypatch.setattr(provider, "current", failing_current)
    with pytest.raises(RuntimeError):
        asyncio.run(get_current_weather(lat=1.0, lon=2.0, tz=DEFAULT_TZ))


def test_fetch_daily_forecasts_validation_errors() -> None:
    with pytest.raises(ValidationError):
        asyncio.run(
            _fetch_daily_forecasts(
                lat=1.0,
                lon=2.0,
                start=date(2025, 2, 2),
                end=date(2025, 2, 1),
                tz=DEFAULT_TZ,
                provider=None,
                endpoint_label="daily",
            )
        )

    start = date(2020, 1, 1)
    end = start + timedelta(days=MAX_RANGE_DAYS + 2)
    with pytest.raises(ValidationError):
        asyncio.run(
            _fetch_daily_forecasts(
                lat=1.0,
                lon=2.0,
                start=start,
                end=end,
                tz=DEFAULT_TZ,
                provider=None,
                endpoint_label="daily",
            )
        )


def test_get_weekly_report_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_cache()
    calls = {"count": 0}

    async def fake_fetch(*_: object, **__: object) -> list[DailyForecast]:
        calls["count"] += 1
        return [
            DailyForecast(
                day=date(2025, 1, 1),
                t_min_c=None,
                t_max_c=None,
                precipitation_mm=None,
                source="open_meteo",
            )
        ]

    monkeypatch.setattr("weather.services._fetch_daily_forecasts", fake_fetch)
    first = asyncio.run(
        get_weekly_report(
            lat=1.0,
            lon=2.0,
            start=date(2025, 1, 1),
            end=date(2025, 1, 1),
            tz=DEFAULT_TZ,
            provider="open_meteo",
        )
    )
    second = asyncio.run(
        get_weekly_report(
            lat=1.0,
            lon=2.0,
            start=date(2025, 1, 1),
            end=date(2025, 1, 1),
            tz=DEFAULT_TZ,
            provider="open_meteo",
        )
    )
    assert calls["count"] == 1
    assert first == second


def test_aggregate_weekly_with_missing_precipitation() -> None:
    forecasts = [
        DailyForecast(
            day=date(2025, 1, 6),
            t_min_c=10.0,
            t_max_c=20.0,
            precipitation_mm=None,
            source="open_meteo",
        )
    ]
    reports = _aggregate_weekly(forecasts, "open_meteo")
    assert reports[0].precipitation_sum_mm is None


def test_cache_key_as_string() -> None:
    key = CacheKey(
        endpoint="current",
        provider="open_meteo",
        lat=1.23456,
        lon=36.78901,
        tz="Africa/Nairobi",
    )
    assert (
        "weather:current:open_meteo:1.2346:36.7890:Africa/Nairobi"
        in key.as_string()
    )


def test_cache_key_with_date_range() -> None:
    key = CacheKey(
        endpoint="daily",
        provider="nasa_power",
        lat=1.0,
        lon=36.0,
        tz="Africa/Nairobi",
        start=date(2025, 1, 1),
        end=date(2025, 1, 10),
    )
    key_str = key.as_string()
    assert "2025-01-01" in key_str
    assert "2025-01-10" in key_str


def test_cache_key_without_date_range() -> None:
    key = CacheKey(
        endpoint="weekly",
        provider="open_meteo",
        lat=-1.0,
        lon=37.0,
        tz="UTC",
    )
    key_str = key.as_string()
    assert ":-:" in key_str


def test_farm_cache_key_as_string() -> None:
    from weather.services import FarmCacheKey

    key = FarmCacheKey(
        endpoint="current",
        provider="open_meteo",
        farm_id=42,
        lat=1.23456,
        lon=36.78901,
        tz="Africa/Nairobi",
    )
    key_str = key.as_string()
    assert "farm-weather:current:open_meteo:42" in key_str
    assert "1.2346:36.7890" in key_str


def test_farm_cache_key_with_hours_days() -> None:
    from weather.services import FarmCacheKey

    key = FarmCacheKey(
        endpoint="hourly",
        provider="nasa_power",
        farm_id=100,
        lat=0.0,
        lon=37.0,
        tz="UTC",
        hours=24,
        days=7,
    )
    key_str = key.as_string()
    assert ":24:7" in key_str


def test_select_provider_valid() -> None:
    from weather.services import _select_provider

    result = _select_provider("open_meteo")
    assert result == "open_meteo"


def test_select_provider_invalid_raises() -> None:
    from weather.services import _select_provider

    with pytest.raises(ValidationError):
        _select_provider("invalid_provider")


def test_select_provider_none_returns_default() -> None:
    from weather.services import _select_provider

    result = _select_provider(None)
    assert result == "open_meteo"


@pytest.mark.django_db
def test_resolve_farm_location_with_centroid() -> None:
    from django.contrib.auth import get_user_model

    from farms.models import Farm

    user_model = get_user_model()
    user = user_model.objects.create_user(
        username="test",
        password=secrets.token_urlsafe(12),
    )
    farm = Farm.objects.create(
        owner=user,
        name="Test Farm",
        slug="test-farm",
        centroid_lat=Decimal("1.5"),
        centroid_lon=Decimal("36.5"),
    )

    location = _resolve_farm_location(farm)
    assert location.lat == 1.5
    assert location.lon == 36.5


@pytest.mark.django_db
def test_resolve_farm_location_with_bbox() -> None:
    from django.contrib.auth import get_user_model

    from farms.models import Farm

    user_model = get_user_model()
    user = user_model.objects.create_user(
        username="test",
        password=secrets.token_urlsafe(12),
    )
    farm = Farm.objects.create(
        owner=user,
        name="Test Farm",
        slug="test-farm",
        centroid_lat=None,
        centroid_lon=None,
        bbox_south=Decimal("1.0"),
        bbox_west=Decimal("36.0"),
        bbox_north=Decimal("2.0"),
        bbox_east=Decimal("37.0"),
    )

    location = _resolve_farm_location(farm)
    assert location.lat == 1.5
    assert location.lon == 36.5


@pytest.mark.django_db
def test_resolve_farm_location_missing_raises() -> None:
    from django.contrib.auth import get_user_model

    from farms.models import Farm

    user_model = get_user_model()
    user = user_model.objects.create_user(
        username="test",
        password=secrets.token_urlsafe(12),
    )
    farm = Farm.objects.create(
        owner=user,
        name="Test Farm",
        slug="test-farm",
        centroid_lat=None,
        centroid_lon=None,
        bbox_south=None,
        bbox_west=None,
        bbox_north=None,
        bbox_east=None,
    )

    with pytest.raises(ValidationError, match="centroid or bounding box"):
        _resolve_farm_location(farm)


def test_stale_and_lock_cache_keys() -> None:
    key = "weather:current:open_meteo:1.0:36.0:Africa/Nairobi:-:-"
    assert _stale_cache_key(key) == f"{key}:stale"
    assert _lock_cache_key(key) == f"{key}:lock"


def test_wait_for_cached_value_found_quickly() -> None:
    import asyncio

    from django.core.cache import cache as default_cache

    _clear_cache()
    key = "test:wait:quick"
    stale_key = f"{key}:stale"
    default_cache.set(key, {"temp": 25.0}, timeout=60)

    async def run_test() -> tuple:
        return await _wait_for_cached_value(
            default_cache, key, stale_key, timeout=1.0
        )

    result, from_stale = asyncio.run(run_test())
    assert result == {"temp": 25.0}
    assert from_stale is False


def test_wait_for_cached_value_from_stale() -> None:
    import asyncio

    from django.core.cache import cache as default_cache

    _clear_cache()
    key = "test:wait:stale"
    stale_key = f"{key}:stale"
    default_cache.set(stale_key, {"temp": 20.0}, timeout=60)

    async def run_test() -> tuple:
        return await _wait_for_cached_value(
            default_cache, key, stale_key, timeout=0.2
        )

    result, from_stale = asyncio.run(run_test())
    assert result == {"temp": 20.0}
    assert from_stale is True


def test_wait_for_cached_value_timeout() -> None:
    import asyncio

    from django.core.cache import cache as default_cache

    _clear_cache()
    key = "test:wait:timeout"
    stale_key = f"{key}:stale"

    async def run_test() -> tuple:
        return await _wait_for_cached_value(
            default_cache, key, stale_key, timeout=0.1
        )

    result, from_stale = asyncio.run(run_test())
    assert result is None
    assert from_stale is False


def test_handle_upstream_error_raises() -> None:
    from weather.services import WeatherUpstreamError

    exc = Exception("upstream failed")
    with pytest.raises(WeatherUpstreamError):
        _handle_upstream_error(exc)


def test_nasa_power_daily_lag_days_default() -> None:
    from weather.serializers import _nasa_power_daily_lag_days

    lag = _nasa_power_daily_lag_days()
    assert lag >= 0


def test_nasa_power_cutoff_date() -> None:
    from datetime import timedelta

    from django.utils import timezone as dj_timezone

    from weather.serializers import _nasa_power_cutoff_date

    cutoff = _nasa_power_cutoff_date()
    expected = dj_timezone.localdate() - timedelta(days=2)
    assert cutoff == expected


def test_missing_fields() -> None:
    from weather.serializers import _missing_fields

    class FakeObj:
        t_min_c = 10.0
        t_max_c = None
        precipitation_mm = 5.0

    missing = _missing_fields(FakeObj())
    assert "t_max_c" in missing
    assert "t_min_c" not in missing


def test_is_missing_day_all_none() -> None:
    from weather.serializers import _is_missing_day

    class FakeObj:
        t_min_c = None
        t_max_c = None
        precipitation_mm = None

    assert _is_missing_day(FakeObj()) is True


def test_is_missing_day_has_data() -> None:
    from weather.serializers import _is_missing_day

    class FakeObj:
        t_min_c = 10.0
        t_max_c = 20.0
        precipitation_mm = 5.0

    assert _is_missing_day(FakeObj()) is False


def test_is_partial_day() -> None:
    from weather.serializers import _is_partial_day

    class FakeObj:
        t_min_c = 10.0
        t_max_c = None
        precipitation_mm = 5.0
        source = "open_meteo"
        day = date(2025, 1, 1)

    assert _is_partial_day(FakeObj()) is True


def test_aggregate_weekly_multiple_weeks() -> None:
    forecasts = [
        DailyForecast(
            day=date(2025, 1, 6),
            t_min_c=10.0,
            t_max_c=20.0,
            precipitation_mm=5.0,
            source="open_meteo",
        ),
        DailyForecast(
            day=date(2025, 1, 13),
            t_min_c=12.0,
            t_max_c=22.0,
            precipitation_mm=3.0,
            source="open_meteo",
        ),
    ]
    reports = _aggregate_weekly(forecasts, "open_meteo")
    assert len(reports) == 2
    assert reports[0].week_start == date(2025, 1, 6)
    assert reports[1].week_start == date(2025, 1, 13)


def test_aggregate_weekly_averages() -> None:
    forecasts = [
        DailyForecast(
            day=date(2025, 1, 6),
            t_min_c=10.0,
            t_max_c=20.0,
            precipitation_mm=5.0,
            source="open_meteo",
        ),
        DailyForecast(
            day=date(2025, 1, 7),
            t_min_c=12.0,
            t_max_c=22.0,
            precipitation_mm=3.0,
            source="open_meteo",
        ),
    ]
    reports = _aggregate_weekly(forecasts, "open_meteo")
    assert len(reports) == 1
    assert reports[0].t_min_avg_c == 11.0
    assert reports[0].t_max_avg_c == 21.0
    assert reports[0].precipitation_sum_mm == 8.0


def test_serialize_daily_with_none_values() -> None:
    from weather.serializers import serialize_daily

    daily = DailyForecast(
        day=date(2025, 1, 1),
        t_min_c=None,
        t_max_c=None,
        precipitation_mm=None,
        wind_speed_max_mps=None,
        source="open_meteo",
    )
    result = serialize_daily([daily])
    assert result[0]["t_min_c"] is None
    assert result[0]["t_max_c"] is None
    assert result[0]["precipitation_mm"] is None


def test_serialize_weekly_with_none_averages() -> None:
    from weather.serializers import serialize_weekly

    weekly = WeeklyReport(
        week_start=date(2025, 1, 6),
        week_end=date(2025, 1, 12),
        t_min_avg_c=None,
        t_max_avg_c=None,
        precipitation_sum_mm=None,
        days=[],
        source="open_meteo",
    )
    result = serialize_weekly([weekly])
    assert result[0]["t_min_avg_c"] is None
    assert result[0]["t_max_avg_c"] is None
    assert result[0]["precipitation_sum_mm"] is None


def test_open_meteo_daily_summary_with_empty_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_cache()

    async def fake_request(
        self: OpenMeteoProvider, params: dict[str, object]
    ) -> dict[str, object]:
        return {"daily": {"time": []}}

    monkeypatch.setattr(OpenMeteoProvider, "_request", fake_request)
    import asyncio

    from weather.services import get_daily_forecast

    result = asyncio.run(
        get_daily_forecast(
            lat=1.0,
            lon=36.0,
            start=date(2025, 1, 1),
            end=date(2025, 1, 1),
            tz="Africa/Nairobi",
            provider="open_meteo",
        )
    )
    assert result == []


def test_nasa_power_daily_with_all_none_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_cache()

    async def fake_request(
        self: NasaPowerProvider, params: dict[str, object]
    ) -> dict[str, object]:
        return {
            "properties": {
                "parameter": {
                    "T2M_MIN": {"20250101": None},
                    "T2M_MAX": {"20250101": None},
                    "PRECTOTCORR": {"20250101": None},
                    "WS10M_MAX": {"20250101": None},
                }
            }
        }

    monkeypatch.setattr(NasaPowerProvider, "_request", fake_request)
    import asyncio

    from weather.services import get_daily_forecast

    result = asyncio.run(
        get_daily_forecast(
            lat=1.0,
            lon=36.0,
            start=date(2025, 1, 1),
            end=date(2025, 1, 1),
            tz="Africa/Nairobi",
            provider="nasa_power",
        )
    )
    assert len(result) == 1
    assert result[0].t_min_c is None


def test_open_meteo_weekly_report_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_cache()
    import asyncio

    from weather.services import get_weekly_report

    payload: dict[str, object] = {
        "daily": {
            "time": ["2025-01-06", "2025-01-07"],
            "temperature_2m_min": [10.0, 12.0],
            "temperature_2m_max": [20.0, 22.0],
            "precipitation_sum": [5.0, 3.0],
            "wind_speed_10m_max": [3.0, 4.0],
        }
    }

    async def fake_request(
        self: OpenMeteoProvider, params: dict[str, object]
    ) -> dict[str, object]:
        return payload

    monkeypatch.setattr(OpenMeteoProvider, "_request", fake_request)

    result = asyncio.run(
        get_weekly_report(
            lat=1.0,
            lon=36.0,
            start=date(2025, 1, 6),
            end=date(2025, 1, 7),
            tz="Africa/Nairobi",
            provider="open_meteo",
        )
    )
    assert len(result) == 1
    assert result[0].t_min_avg_c == 11.0
    assert result[0].t_max_avg_c == 21.0


def test_nasa_power_weekly_report_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_cache()
    import asyncio

    from weather.services import get_weekly_report

    payload: dict[str, object] = {
        "properties": {
            "parameter": {
                "T2M_MIN": {"20250106": 10.0, "20250107": 12.0},
                "T2M_MAX": {"20250106": 20.0, "20250107": 22.0},
                "PRECTOTCORR": {"20250106": 0.001, "20250107": 0.002},
                "WS10M_MAX": {"20250106": 3.0, "20250107": 4.0},
            }
        }
    }

    async def fake_request(
        self: NasaPowerProvider, params: dict[str, object]
    ) -> dict[str, object]:
        return payload

    monkeypatch.setattr(NasaPowerProvider, "_request", fake_request)

    result = asyncio.run(
        get_weekly_report(
            lat=1.0,
            lon=36.0,
            start=date(2025, 1, 6),
            end=date(2025, 1, 7),
            tz="Africa/Nairobi",
            provider="nasa_power",
        )
    )
    assert len(result) == 1


def test_get_current_with_stale_cache_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_cache()
    import asyncio

    from django.core.cache import cache as default_cache

    from weather.engines.types import CurrentWeather
    from weather.services import (
        PROVIDER_REGISTRY,
        get_current_weather,
    )

    # Set stale value
    stale_weather = CurrentWeather(
        temperature_c=20.0,
        wind_speed_mps=3.0,
        observed_at=datetime.now(ZoneInfo("Africa/Nairobi")),
        source="open_meteo",
    )

    lat, lon, tz = 1.0, 36.0, "Africa/Nairobi"
    key_str = f"weather:current:open_meteo:{lat:.4f}:{lon:.4f}:{tz}:-:-"
    stale_key = _stale_cache_key(key_str)
    lock_key = _lock_cache_key(key_str)

    # Set stale value and acquire lock to simulate another request fetching
    default_cache.set(stale_key, stale_weather, timeout=60)
    default_cache.set(lock_key, 1, timeout=60)

    # Make provider raise error
    async def failing_current(
        *args: object, **kwargs: object
    ) -> CurrentWeather:
        raise Exception("upstream error")

    monkeypatch.setattr(
        PROVIDER_REGISTRY["open_meteo"], "current", failing_current
    )
    # Shorten wait timeout for test
    monkeypatch.setattr("weather.services.CACHE_LOCK_WAIT_SECONDS", 0.2)

    # Should return stale value after waiting for lock holder
    result = asyncio.run(
        get_current_weather(
            lat=lat,
            lon=lon,
            tz=tz,
            provider="open_meteo",
        )
    )
    assert result.temperature_c == 20.0


def test_open_meteo_hourly_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_cache()
    payload: dict[str, object] = {
        "hourly": {
            "time": ["2025-01-01T00:00", "2025-01-01T01:00"],
            "temperature_2m": [20.0, 21.0],
            "precipitation": [0.0, 1.5],
            "wind_speed_10m": [3.0, 4.0],
            "cloudcover": [50.0, 60.0],
        }
    }

    async def fake_request(
        self: OpenMeteoProvider, params: dict[str, object]
    ) -> dict[str, object]:
        return payload

    monkeypatch.setattr(OpenMeteoProvider, "_request", fake_request)
    provider = OpenMeteoProvider()
    forecasts = asyncio.run(
        provider.hourly(
            Location(lat=1.0, lon=36.0, tz="Africa/Nairobi"),
            hours=2,
        )
    )
    assert len(forecasts) == 2
    first = forecasts[0]
    assert first.temperature_c == pytest.approx(20.0)
    assert first.precipitation_mm == pytest.approx(0.0)
    assert first.wind_speed_mps == pytest.approx(3.0)
    assert first.cloud_cover_pct == pytest.approx(50.0)


def test_open_meteo_hourly_with_missing_cloudcover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_cache()
    payload: dict[str, object] = {
        "hourly": {
            "time": ["2025-01-01T00:00"],
            "temperature_2m": [20.0],
            "precipitation": [0.0],
            "wind_speed_10m": [3.0],
        }
    }

    async def fake_request(
        self: OpenMeteoProvider, params: dict[str, object]
    ) -> dict[str, object]:
        return payload

    monkeypatch.setattr(OpenMeteoProvider, "_request", fake_request)
    provider = OpenMeteoProvider()
    forecasts = asyncio.run(
        provider.hourly(
            Location(lat=1.0, lon=36.0, tz="Africa/Nairobi"),
            hours=1,
        )
    )
    assert len(forecasts) == 1
    assert forecasts[0].cloud_cover_pct is None


def test_open_meteo_daily_summary_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_cache()
    payload: dict[str, object] = {
        "daily": {
            "time": ["2025-01-01", "2025-01-02"],
            "temperature_2m_min": [10.0, 12.0],
            "temperature_2m_max": [20.0, 22.0],
            "precipitation_sum": [5.0, 3.0],
            "wind_speed_10m_max": [3.0, 4.0],
        }
    }

    async def fake_request(
        self: OpenMeteoProvider, params: dict[str, object]
    ) -> dict[str, object]:
        return payload

    monkeypatch.setattr(OpenMeteoProvider, "_request", fake_request)
    provider = OpenMeteoProvider()
    summaries = asyncio.run(
        provider.daily_summary(
            Location(lat=1.0, lon=36.0, tz="Africa/Nairobi"),
            date(2025, 1, 1),
            date(2025, 1, 2),
        )
    )
    assert len(summaries) == 2
    first = summaries[0]
    assert first.t_min_c == pytest.approx(10.0)
    assert first.t_max_c == pytest.approx(20.0)
    assert first.precipitation_mm == pytest.approx(5.0)
    assert first.wind_speed_max_mps == pytest.approx(3.0)


def test_open_meteo_current_with_missing_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_cache()
    payload: dict[str, object] = {
        "current": {
            "time": "2025-01-01T10:00",
            "temperature_2m": None,
            "wind_speed_10m": None,
        }
    }

    async def fake_request(
        self: OpenMeteoProvider, params: dict[str, object]
    ) -> dict[str, object]:
        return payload

    monkeypatch.setattr(OpenMeteoProvider, "_request", fake_request)
    provider = OpenMeteoProvider()
    result = asyncio.run(
        provider.current(Location(lat=1.0, lon=36.0, tz="Africa/Nairobi"))
    )
    assert result.temperature_c is None
    assert result.wind_speed_mps is None


@pytest.mark.django_db
def test_get_farm_current_weather_cache_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_cache()
    from django.contrib.auth import get_user_model

    from farms.models import Farm

    user = get_user_model().objects.create_user(
        username="farm-weather-user",
        password=secrets.token_urlsafe(12),
    )
    farm = Farm.objects.create(
        owner=user,
        name="Test Farm",
        slug="test-farm",
        centroid_lat=Decimal("1.5"),
        centroid_lon=Decimal("36.5"),
    )

    cached_weather = CurrentWeather(
        observed_at=datetime(2025, 1, 1, tzinfo=UTC),
        temperature_c=25.0,
        wind_speed_mps=5.0,
        source="open_meteo",
    )
    key = (
        f"farm-weather:current:open_meteo:{farm.id}:"
        f"1.5000:36.5000:Africa/Nairobi:-:-"
    )
    caches["default"].set(key, cached_weather, 60)

    result = asyncio.run(get_farm_current_weather(farm, provider="open_meteo"))
    assert result.temperature_c == 25.0


@pytest.mark.django_db
def test_get_farm_hourly_forecast_cache_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_cache()
    from django.contrib.auth import get_user_model

    from farms.models import Farm
    from weather.engines.types import HourlyForecast

    user = get_user_model().objects.create_user(
        username="farm-hourly-user",
        password=secrets.token_urlsafe(12),
    )
    farm = Farm.objects.create(
        owner=user,
        name="Test Farm",
        slug="test-farm",
        centroid_lat=Decimal("1.5"),
        centroid_lon=Decimal("36.5"),
    )

    cached_hourly = [
        HourlyForecast(
            timestamp=datetime(2025, 1, 1, tzinfo=UTC),
            temperature_c=20.0,
            precipitation_mm=0.0,
            wind_speed_mps=3.0,
            cloud_cover_pct=50.0,
            source="open_meteo",
        )
    ]
    key = (
        f"farm-weather:hourly:open_meteo:{farm.id}:"
        f"1.5000:36.5000:Africa/Nairobi:24:-"
    )
    caches["default"].set(key, cached_hourly, 600)

    result = asyncio.run(
        get_farm_hourly_forecast(farm, hours=24, provider="open_meteo")
    )
    assert result == cached_hourly


@pytest.mark.django_db
def test_get_farm_daily_summary_cache_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_cache()
    from django.contrib.auth import get_user_model

    from farms.models import Farm
    from weather.engines.types import DailySummary

    user = get_user_model().objects.create_user(
        username="farm-daily-user",
        password=secrets.token_urlsafe(12),
    )
    farm = Farm.objects.create(
        owner=user,
        name="Test Farm",
        slug="test-farm",
        centroid_lat=Decimal("1.5"),
        centroid_lon=Decimal("36.5"),
    )

    cached_daily = [
        DailySummary(
            day=date(2025, 1, 1),
            t_min_c=10.0,
            t_max_c=20.0,
            precipitation_mm=5.0,
            wind_speed_max_mps=3.0,
            source="open_meteo",
        )
    ]
    key = (
        f"farm-weather:daily:open_meteo:{farm.id}:"
        f"1.5000:36.5000:Africa/Nairobi:-:7"
    )
    caches["default"].set(key, cached_daily, 1800)

    result = asyncio.run(
        get_farm_daily_summary(farm, days=7, provider="open_meteo")
    )
    assert result == cached_daily


@pytest.mark.django_db
def test_get_farm_current_weather_error_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_cache()
    from django.contrib.auth import get_user_model

    from farms.models import Farm

    user = get_user_model().objects.create_user(
        username="farm-error-user",
        password=secrets.token_urlsafe(12),
    )
    farm = Farm.objects.create(
        owner=user,
        name="Test Farm",
        slug="test-farm",
        centroid_lat=Decimal("1.5"),
        centroid_lon=Decimal("36.5"),
    )

    async def failing_current(*_: object, **__: object) -> None:
        raise httpx.HTTPError("upstream failed")

    provider = PROVIDER_REGISTRY["open_meteo"]
    monkeypatch.setattr(provider, "current", failing_current)

    with pytest.raises(Exception):  # noqa: B017
        asyncio.run(get_farm_current_weather(farm, provider="open_meteo"))


# =============================================================================
# Additional tests for uncovered lines in weather/services.py
# =============================================================================


def test_farm_cache_key_as_string_with_hours() -> None:
    """Test FarmCacheKey.as_string() with hours parameter."""
    key = FarmCacheKey(
        endpoint="hourly",
        provider="open_meteo",
        farm_id=123,
        lat=1.5,
        lon=36.5,
        tz="Africa/Nairobi",
        hours=24,
        days=None,
    )
    result = key.as_string()
    assert "farm-weather:hourly:open_meteo:123" in result
    assert "1.5000:36.5000:Africa/Nairobi:24:-" in result


def test_farm_cache_key_as_string_with_days() -> None:
    """Test FarmCacheKey.as_string() with days parameter."""
    key = FarmCacheKey(
        endpoint="daily",
        provider="nasa_power",
        farm_id=456,
        lat=-1.0,
        lon=37.0,
        tz="UTC",
        hours=None,
        days=7,
    )
    result = key.as_string()
    assert "farm-weather:daily:nasa_power:456" in result
    assert "-1.0000:37.0000:UTC:-:7" in result


def test_cache_key_with_dates() -> None:
    """Test CacheKey.as_string() with start and end dates."""
    start = date(2025, 1, 1)
    end = date(2025, 1, 7)
    key = CacheKey(
        endpoint="daily",
        provider="open_meteo",
        lat=1.0,
        lon=36.0,
        tz="Africa/Nairobi",
        start=start,
        end=end,
    )
    result = key.as_string()
    assert "2025-01-01:2025-01-07" in result


def test_stale_and_lock_cache_keys_roundtrip() -> None:
    """Test _stale_cache_key and _lock_cache_key functions."""

    base_key = "weather:current:open_meteo:1.0:36.0:Africa/Nairobi"
    assert _stale_cache_key(base_key) == f"{base_key}:stale"
    assert _lock_cache_key(base_key) == f"{base_key}:lock"


def test_wait_for_cached_value_returns_cached() -> None:
    """Test _wait_for_cached_value when value is already cached."""
    from django.core.cache import cache as default_cache

    async def _run_test() -> None:
        _clear_cache()
        test_value = CurrentWeather(
            observed_at=datetime(2025, 1, 1, tzinfo=UTC),
            temperature_c=25.0,
            wind_speed_mps=3.0,
            source="open_meteo",
        )
        key = "test:wait:for:cached"
        default_cache.set(key, test_value, 60)

        result, from_stale = await _wait_for_cached_value(
            default_cache, key, f"{key}:stale", timeout=1.0
        )
        assert result == test_value
        assert from_stale is False
        default_cache.delete(key)

    asyncio.run(_run_test())


def test_wait_for_cached_value_returns_stale() -> None:
    """Test _wait_for_cached_value when only stale value exists."""
    from django.core.cache import cache as default_cache

    async def _run_test() -> None:
        _clear_cache()
        stale_value = CurrentWeather(
            observed_at=datetime(2025, 1, 1, tzinfo=UTC),
            temperature_c=24.0,
            wind_speed_mps=2.5,
            source="nasa_power",
        )
        key = "test:wait:stale"
        stale_key = f"{key}:stale"
        default_cache.set(stale_key, stale_value, 60)

        result, from_stale = await _wait_for_cached_value(
            default_cache, key, stale_key, timeout=0.2
        )
        assert result == stale_value
        assert from_stale is True
        default_cache.delete(stale_key)

    asyncio.run(_run_test())


def test_wait_for_cached_value_returns_none() -> None:
    """Test _wait_for_cached_value when no value exists."""
    from django.core.cache import cache as default_cache

    async def _run_test() -> None:
        _clear_cache()
        key = "test:wait:none"
        stale_key = f"{key}:stale"

        result, from_stale = await _wait_for_cached_value(
            default_cache, key, stale_key, timeout=0.1
        )
        assert result is None
        assert from_stale is False

    asyncio.run(_run_test())


def test_handle_upstream_error() -> None:
    """Test _handle_upstream_error raises WeatherUpstreamError."""
    from weather.services import WeatherUpstreamError

    test_exc = ValueError("test error")
    with pytest.raises(WeatherUpstreamError) as exc_info:
        _handle_upstream_error(test_exc)
    assert exc_info.value.__cause__ is test_exc


@pytest.mark.django_db
def test_get_current_weather_cache_hit_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test get_current_weather returns cached value."""
    _clear_cache()

    from weather.services import get_current_weather

    cached_value = CurrentWeather(
        observed_at=datetime(2025, 1, 1, tzinfo=UTC),
        temperature_c=22.0,
        wind_speed_mps=4.0,
        source="open_meteo",
    )
    key_str = "weather:current:open_meteo:1.0000:36.0000:Africa/Nairobi:-:-"
    caches["default"].set(key_str, cached_value, 120)

    result = asyncio.run(
        get_current_weather(lat=1.0, lon=36.0, provider="open_meteo")
    )
    assert result == cached_value


@pytest.mark.django_db
def test_get_current_weather_lock_wait_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test get_current_weather waits for lock and returns stale value."""
    _clear_cache()

    from weather.services import get_current_weather

    stale_value = CurrentWeather(
        observed_at=datetime(2025, 1, 1, tzinfo=UTC),
        temperature_c=23.0,
        wind_speed_mps=3.5,
        source="open_meteo",
    )
    key_str = "weather:current:open_meteo:2.0000:37.0000:Africa/Nairobi:-:-"
    stale_key = f"{key_str}:stale"
    lock_key = f"{key_str}:lock"
    caches["default"].set(stale_key, stale_value, 300)
    caches["default"].set(lock_key, 1, timeout=10)

    async def slow_current(*_: object, **__: object) -> CurrentWeather:
        await asyncio.sleep(0.5)
        return CurrentWeather(
            observed_at=datetime(2025, 1, 2, tzinfo=UTC),
            temperature_c=24.0,
            wind_speed_mps=4.0,
            source="open_meteo",
        )

    provider = PROVIDER_REGISTRY["open_meteo"]
    monkeypatch.setattr(provider, "current", slow_current)

    result = asyncio.run(
        get_current_weather(lat=2.0, lon=37.0, provider="open_meteo")
    )
    assert result == stale_value
    caches["default"].delete(lock_key)


@pytest.mark.django_db
def test_get_current_weather_stale_fallback_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test get_current_weather returns stale value on upstream error."""
    _clear_cache()

    from weather.services import get_current_weather

    stale_value = CurrentWeather(
        observed_at=datetime(2025, 1, 1, tzinfo=UTC),
        temperature_c=21.0,
        wind_speed_mps=3.0,
        source="open_meteo",
    )
    key_str = "weather:current:open_meteo:3.0000:38.0000:Africa/Nairobi:-:-"
    stale_key = f"{key_str}:stale"
    caches["default"].set(stale_key, stale_value, 300)

    async def failing_current(*_: object, **__: object) -> None:
        raise httpx.HTTPError("upstream failed")

    provider = PROVIDER_REGISTRY["open_meteo"]
    monkeypatch.setattr(provider, "current", failing_current)

    result = asyncio.run(
        get_current_weather(lat=3.0, lon=38.0, provider="open_meteo")
    )
    assert result == stale_value


@pytest.mark.django_db
def test_resolve_farm_location_with_centroid_override() -> None:
    """Test _resolve_farm_location uses centroid when available."""
    from django.contrib.auth import get_user_model

    from farms.models import Farm

    user = get_user_model().objects.create_user(
        username="centroid-user",
        password=secrets.token_urlsafe(12),
    )
    farm = Farm.objects.create(
        owner=user,
        name="Centroid Farm",
        slug="centroid-farm",
        centroid_lat=Decimal("-1.2921"),
        centroid_lon=Decimal("36.8219"),
    )

    location = _resolve_farm_location(farm)
    assert location.lat == -1.2921
    assert location.lon == 36.8219


@pytest.mark.django_db
def test_resolve_farm_location_with_bbox_override() -> None:
    """Ensure _resolve_farm_location falls back to bbox centroid."""
    from django.contrib.auth import get_user_model

    from farms.models import Farm

    user = get_user_model().objects.create_user(
        username="bbox-user",
        password=secrets.token_urlsafe(12),
    )
    farm = Farm.objects.create(
        owner=user,
        name="BBox Farm",
        slug="bbox-farm",
        centroid_lat=None,
        centroid_lon=None,
        bbox_south=Decimal("-2.0"),
        bbox_west=Decimal("35.0"),
        bbox_north=Decimal("-1.0"),
        bbox_east=Decimal("37.0"),
    )

    location = _resolve_farm_location(farm)
    assert location.lat == -1.5  # (-2.0 + -1.0) / 2
    assert location.lon == 36.0  # (35.0 + 37.0) / 2


@pytest.mark.django_db
def test_resolve_farm_location_error() -> None:
    """Ensure _resolve_farm_location raises when location data is missing."""
    from django.contrib.auth import get_user_model
    from rest_framework.exceptions import ValidationError

    from farms.models import Farm

    user = get_user_model().objects.create_user(
        username="no-location-user",
        password=secrets.token_urlsafe(12),
    )
    farm = Farm.objects.create(
        owner=user,
        name="No Location Farm",
        slug="no-location-farm",
        centroid_lat=None,
        centroid_lon=None,
        bbox_south=None,
        bbox_west=None,
        bbox_north=None,
        bbox_east=None,
    )

    with pytest.raises(ValidationError) as exc_info:
        _resolve_farm_location(farm)
    assert "Farm must have a centroid or bounding box" in str(exc_info.value)


@pytest.mark.django_db
def test_get_farm_current_weather_cache_hit_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test get_farm_current_weather returns cached value."""
    _clear_cache()

    from django.contrib.auth import get_user_model

    from farms.models import Farm

    user = get_user_model().objects.create_user(
        username="farm-cache-user",
        password=secrets.token_urlsafe(12),
    )
    farm = Farm.objects.create(
        owner=user,
        name="Cache Test Farm",
        slug="cache-test-farm",
        centroid_lat=Decimal("0.5"),
        centroid_lon=Decimal("36.0"),
    )

    cached_value = CurrentWeather(
        observed_at=datetime(2025, 1, 1, tzinfo=UTC),
        temperature_c=26.0,
        wind_speed_mps=2.0,
        source="open_meteo",
    )
    key = (
        f"farm-weather:current:open_meteo:{farm.id}:"
        f"0.5000:36.0000:Africa/Nairobi:-:-"
    )
    caches["default"].set(key, cached_value, 60)

    result = asyncio.run(get_farm_current_weather(farm, provider="open_meteo"))
    assert result == cached_value


def test_select_provider_invalid() -> None:
    """Test _select_provider raises ValidationError for invalid provider."""
    from rest_framework.exceptions import ValidationError

    from weather.services import _select_provider

    with pytest.raises(ValidationError) as exc_info:
        _select_provider("invalid_provider_xyz")
    assert "invalid_provider_xyz" in str(exc_info.value)


def test_aggregate_weekly_empty() -> None:
    """Test _aggregate_weekly with empty forecasts list."""
    from weather.services import _aggregate_weekly

    result = _aggregate_weekly([], "open_meteo")
    assert result == []


def test_aggregate_weekly_single_day() -> None:
    """Test _aggregate_weekly with single forecast."""
    from weather.services import _aggregate_weekly

    forecasts = [
        DailyForecast(
            day=date(2025, 1, 6),  # Monday
            t_min_c=15.0,
            t_max_c=25.0,
            precipitation_mm=5.0,
            source="open_meteo",
            wind_speed_max_mps=4.0,
        )
    ]
    result = _aggregate_weekly(forecasts, "open_meteo")
    assert len(result) == 1
    assert result[0].week_start == date(2025, 1, 6)
    assert result[0].week_end == date(2025, 1, 12)
    assert result[0].t_min_avg_c == 15.0
    assert result[0].t_max_avg_c == 25.0
    assert result[0].precipitation_sum_mm == 5.0


def test_aggregate_weekly_multiple_weeks_continued() -> None:
    """Test _aggregate_weekly spans multiple weeks."""
    from weather.services import _aggregate_weekly

    forecasts = [
        DailyForecast(
            day=date(2025, 1, 5),  # Sunday (week before)
            t_min_c=10.0,
            t_max_c=20.0,
            precipitation_mm=0.0,
            source="open_meteo",
            wind_speed_max_mps=3.0,
        ),
        DailyForecast(
            day=date(2025, 1, 6),  # Monday (new week)
            t_min_c=15.0,
            t_max_c=25.0,
            precipitation_mm=5.0,
            source="open_meteo",
            wind_speed_max_mps=4.0,
        ),
    ]
    result = _aggregate_weekly(forecasts, "open_meteo")
    assert len(result) == 2
    assert result[0].week_start == date(2024, 12, 30)
    assert result[1].week_start == date(2025, 1, 6)


def test_aggregate_weekly_with_nulls() -> None:
    """Test _aggregate_weekly handles None values correctly."""
    from weather.services import _aggregate_weekly

    forecasts = [
        DailyForecast(
            day=date(2025, 1, 6),
            t_min_c=None,
            t_max_c=None,
            precipitation_mm=None,
            source="open_meteo",
            wind_speed_max_mps=None,
        )
    ]
    result = _aggregate_weekly(forecasts, "open_meteo")
    assert len(result) == 1
    assert result[0].t_min_avg_c is None
    assert result[0].t_max_avg_c is None
    assert result[0].precipitation_sum_mm is None


# =============================================================================
# Additional tests for uncovered lines in weather/engines/open_meteo.py
# =============================================================================


def test_open_meteo_daily_summary_with_empty_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test OpenMeteoProvider.daily_summary handles empty daily block."""
    _clear_cache()

    async def _run_test() -> None:
        async def fake_request(
            self: OpenMeteoProvider, params: dict[str, object]
        ) -> dict[str, object]:
            return {"daily": {}}

        monkeypatch.setattr(OpenMeteoProvider, "_request", fake_request)
        provider = OpenMeteoProvider()
        location = Location(lat=1.0, lon=36.0, tz="Africa/Nairobi")
        start = date(2025, 1, 1)
        end = date(2025, 1, 7)

        result = await provider.daily_summary(location, start, end)
        assert result == []

    asyncio.run(_run_test())


def test_open_meteo_hourly_with_empty_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test OpenMeteoProvider.hourly handles empty hourly block."""
    _clear_cache()

    async def _run_test() -> None:
        async def fake_request(
            self: OpenMeteoProvider, params: dict[str, object]
        ) -> dict[str, object]:
            return {"hourly": {}}

        monkeypatch.setattr(OpenMeteoProvider, "_request", fake_request)
        provider = OpenMeteoProvider()
        location = Location(lat=1.0, lon=36.0, tz="Africa/Nairobi")

        result = await provider.hourly(location, hours=24)
        assert result == []

    asyncio.run(_run_test())


def test_open_meteo_daily_with_non_dict_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test OpenMeteoProvider.daily handles non-dict payload."""
    _clear_cache()

    async def _run_test() -> None:
        async def fake_request(
            self: OpenMeteoProvider, params: dict[str, object]
        ) -> dict[str, object]:
            return {}  # No 'daily' key

        monkeypatch.setattr(OpenMeteoProvider, "_request", fake_request)
        provider = OpenMeteoProvider()
        location = Location(lat=1.0, lon=36.0, tz="Africa/Nairobi")
        start = date(2025, 1, 1)
        end = date(2025, 1, 7)

        result = await provider.daily(location, start, end)
        assert result == []

    asyncio.run(_run_test())


def test_open_meteo_parse_datetime_invalid() -> None:
    """Test OpenMeteoProvider._parse_datetime handles invalid formats."""
    provider = OpenMeteoProvider()
    zone = ZoneInfo("Africa/Nairobi")

    assert provider._parse_datetime(None, zone) is None  # type: ignore[arg-type]
    assert provider._parse_datetime(123, zone) is None  # type: ignore[arg-type]
    assert provider._parse_datetime("not-a-date", zone) is None


def test_open_meteo_parse_date_invalid() -> None:
    """Test OpenMeteoProvider._parse_date handles invalid formats."""
    provider = OpenMeteoProvider()

    assert provider._parse_date(None) is None  # type: ignore[arg-type]
    assert provider._parse_date(123) is None  # type: ignore[arg-type]
    assert provider._parse_date("not-a-date") is None


def test_open_meteo_list_value_out_of_range() -> None:
    """Test OpenMeteoProvider._list_value when index is out of range."""
    provider = OpenMeteoProvider()
    values = [1.0, 2.0, 3.0]

    assert provider._list_value(values, 10) is None


def test_open_meteo_to_float_invalid() -> None:
    """Test OpenMeteoProvider._to_float handles invalid values."""
    provider = OpenMeteoProvider()

    assert provider._to_float(None) is None
    assert provider._to_float("not-a-number") is None
    assert provider._to_float(object()) is None  # type: ignore[arg-type]


def test_open_meteo_current_missing_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure OpenMeteoProvider.current uses now when response lacks time."""
    _clear_cache()

    async def _run_test() -> None:
        async def fake_request(
            self: OpenMeteoProvider, params: dict[str, object]
        ) -> dict[str, object]:
            return {"current": {"temperature_2m": 22.0, "wind_speed_10m": 3.0}}

        monkeypatch.setattr(OpenMeteoProvider, "_request", fake_request)
        provider = OpenMeteoProvider()
        location = Location(lat=1.0, lon=36.0, tz="Africa/Nairobi")

        result = await provider.current(location)
        assert result.temperature_c == 22.0
        assert result.wind_speed_mps == 3.0
        assert result.source == "open_meteo"

    asyncio.run(_run_test())
