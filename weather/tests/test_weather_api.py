from __future__ import annotations

# ruff: noqa: S101
import secrets
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
from django.contrib.auth import get_user_model
from django.core.cache import caches
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory, force_authenticate

from weather.engines.types import CurrentWeather, DailyForecast, WeeklyReport
from weather.views import (
    WeatherCurrentView,
    WeatherDailyView,
    WeatherWeeklyView,
)


@pytest.mark.django_db
def test_weather_current_view_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = get_user_model().objects.create_user(
        username="weather-user",
        email="weather@example.com",
        password=secrets.token_urlsafe(12),
    )
    factory = APIRequestFactory()

    captured: dict[str, object] = {}

    async def fake_get_current_weather(
        *,
        lat: float,
        lon: float,
        tz: str,
        provider: str | None,
    ) -> CurrentWeather:
        captured["provider"] = provider
        return CurrentWeather(
            observed_at=datetime(2025, 1, 1, tzinfo=UTC),
            temperature_c=22.0,
            wind_speed_mps=4.0,
            source="open_meteo",
        )

    monkeypatch.setattr(
        "weather.views.get_current_weather", fake_get_current_weather
    )
    django_request = factory.get(
        "/api/v1/weather/current/",
        {"lat": "1.0", "lon": "36.0", "tz": "UTC", "provider": "open_meteo"},
    )
    force_authenticate(django_request, user=user)
    request = Request(django_request)
    resp = WeatherCurrentView().get(request)
    assert resp.status_code == 200
    assert resp.data["status"] == 0
    assert resp.data["data"]["temperature_c"] == 22.0
    assert captured["provider"] == "open_meteo"


@pytest.mark.django_db
def test_weather_daily_view_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = get_user_model().objects.create_user(
        username="weather-daily",
        email="weather-daily@example.com",
        password=secrets.token_urlsafe(12),
    )
    factory = APIRequestFactory()

    async def fake_get_daily_forecast(
        *,
        lat: float,
        lon: float,
        start: date,
        end: date,
        tz: str,
        provider: str | None,
    ) -> list[DailyForecast]:
        return [
            DailyForecast(
                day=start,
                t_min_c=12.0,
                t_max_c=20.0,
                precipitation_mm=None,
                source="open_meteo",
            )
        ]

    monkeypatch.setattr(
        "weather.views.get_daily_forecast", fake_get_daily_forecast
    )
    django_request = factory.get(
        "/api/v1/weather/daily/",
        {
            "lat": "1.0",
            "lon": "36.0",
            "start": "2025-01-01",
            "end": "2025-01-01",
        },
    )
    force_authenticate(django_request, user=user)
    request = Request(django_request)
    resp = WeatherDailyView().get(request)
    assert resp.status_code == 200
    assert resp.data["status"] == 0
    assert len(resp.data["data"]["forecasts"]) == 1


@pytest.mark.django_db
def test_weather_weekly_view_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = get_user_model().objects.create_user(
        username="weather-weekly",
        email="weather-weekly@example.com",
        password=secrets.token_urlsafe(12),
    )
    factory = APIRequestFactory()

    async def fake_get_weekly_report(
        *,
        lat: float,
        lon: float,
        start: date,
        end: date,
        tz: str,
        provider: str | None,
    ) -> list[WeeklyReport]:
        daily = DailyForecast(
            day=start,
            t_min_c=10.0,
            t_max_c=20.0,
            precipitation_mm=1.0,
            source="open_meteo",
        )
        return [
            WeeklyReport(
                week_start=start,
                week_end=end,
                t_min_avg_c=10.0,
                t_max_avg_c=20.0,
                precipitation_sum_mm=1.0,
                days=[daily],
                source="open_meteo",
            )
        ]

    monkeypatch.setattr(
        "weather.views.get_weekly_report", fake_get_weekly_report
    )
    django_request = factory.get(
        "/api/v1/weather/weekly/",
        {
            "lat": "1.0",
            "lon": "36.0",
            "start": "2025-01-01",
            "end": "2025-01-07",
        },
    )
    force_authenticate(django_request, user=user)
    request = Request(django_request)
    resp = WeatherWeeklyView().get(request)
    assert resp.status_code == 200
    assert resp.data["status"] == 0
    assert len(resp.data["data"]["reports"]) == 1


@pytest.mark.django_db
def test_weather_daily_view_proxy_cache(
    monkeypatch: pytest.MonkeyPatch,
    settings: Any,
) -> None:
    user = get_user_model().objects.create_user(
        username="weather-proxy",
        email="weather-proxy@example.com",
        password=secrets.token_urlsafe(12),
    )
    factory = APIRequestFactory()

    settings.WEATHER_PROXY_ENABLED = True
    settings.WEATHER_SERVICE_URL = "http://weather-service:8090"
    settings.PROXY_TIMEOUT_SECONDS = 5.0
    settings.WEATHER_CACHE_TTL_DAILY_S = 900
    caches["default"].clear()
    calls = 0

    def fake_request(
        method: str,
        url: str,
        params: dict[str, str] | None = None,
        json: object | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> object:
        nonlocal calls
        calls += 1
        assert method == "GET"
        assert url == "http://weather-service:8090/api/v1/weather/daily/"
        assert params is not None
        assert params["lat"] == "1.0"
        assert params["lon"] == "36.0"
        assert params["start"] == "2025-01-01"
        assert params["end"] == "2025-01-01"
        return httpx.Response(
            status_code=200,
            json={
                "status": 0,
                "message": "OK",
                "data": {
                    "forecasts": [
                        {
                            "day": "2025-01-01",
                            "t_min_c": 12.0,
                            "t_max_c": 20.0,
                            "precipitation_mm": 0.0,
                            "wind_speed_max_mps": 3.0,
                            "is_partial": False,
                            "missing_fields": [],
                            "source": "open_meteo",
                        }
                    ]
                },
                "errors": None,
            },
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr("config.api.proxy.httpx.request", fake_request)

    first_request = factory.get(
        "/api/v1/weather/daily/",
        {
            "lat": "1.0",
            "lon": "36.0",
            "start": "2025-01-01",
            "end": "2025-01-01",
        },
    )
    force_authenticate(first_request, user=user)
    first = WeatherDailyView().get(Request(first_request))

    second_request = factory.get(
        "/api/v1/weather/daily/",
        {
            "lat": "1.0",
            "lon": "36.0",
            "start": "2025-01-01",
            "end": "2025-01-01",
        },
    )
    force_authenticate(second_request, user=user)
    second = WeatherDailyView().get(Request(second_request))

    assert first.status_code == 200
    assert second.status_code == 200
    assert calls == 1
