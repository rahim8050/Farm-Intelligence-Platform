from __future__ import annotations

# ruff: noqa: S101
import secrets
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APIClient

from farms.models import Farm
from integrations.tokens import mint_integration_access_token
from weather.engines.types import (
    CurrentWeather,
    DailySummary,
    HourlyForecast,
)
from weather.services import WeatherUpstreamError


def _auth_client() -> APIClient:
    access, _ = mint_integration_access_token(user_id="client-1", scope="read")
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    return client


def _create_farm() -> Farm:
    user = get_user_model().objects.create_user(
        username="farm-owner",
        email="farm-owner@example.com",
        password=secrets.token_urlsafe(12),
    )
    return Farm.objects.create(
        owner=user,
        name="Farm Weather",
        centroid_lat=Decimal("1.25"),
        centroid_lon=Decimal("36.75"),
    )


@pytest.mark.django_db
def test_farm_weather_current_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    farm = _create_farm()
    client = _auth_client()

    async def fake_current(*, farm: Farm) -> CurrentWeather:
        return CurrentWeather(
            observed_at=datetime(2025, 1, 1, tzinfo=UTC),
            temperature_c=23.5,
            wind_speed_mps=5.2,
            source="open_meteo",
        )

    monkeypatch.setattr(
        "weather.farm_views.get_farm_current_weather", fake_current
    )

    resp = client.get(f"/api/v1/farms/{farm.id}/weather/current/")

    assert resp.status_code == status.HTTP_200_OK
    body = resp.json()
    assert body["status"] == 0
    assert body["data"]["temperature_c"] == 23.5


@pytest.mark.django_db
def test_farm_weather_current_invalid_params() -> None:
    farm = _create_farm()
    client = _auth_client()

    resp = client.get(
        f"/api/v1/farms/{farm.id}/weather/current/",
        {"hours": "12"},
    )

    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert resp.json()["status"] == 1


@pytest.mark.django_db
def test_farm_weather_current_missing_farm() -> None:
    client = _auth_client()

    resp = client.get("/api/v1/farms/99999/weather/current/")

    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["status"] == 1


@pytest.mark.django_db
def test_farm_weather_current_upstream_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    farm = _create_farm()
    client = _auth_client()

    async def fake_current(*, farm: Farm) -> CurrentWeather:
        raise WeatherUpstreamError()

    monkeypatch.setattr(
        "weather.farm_views.get_farm_current_weather", fake_current
    )

    resp = client.get(f"/api/v1/farms/{farm.id}/weather/current/")

    assert resp.status_code == status.HTTP_502_BAD_GATEWAY
    assert resp.json()["status"] == 1


@pytest.mark.django_db
def test_farm_weather_hourly_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    farm = _create_farm()
    client = _auth_client()

    async def fake_hourly(*, farm: Farm, hours: int) -> list[HourlyForecast]:
        assert hours == 48
        return [
            HourlyForecast(
                timestamp=datetime(2025, 1, 1, tzinfo=UTC),
                temperature_c=20.0,
                precipitation_mm=0.2,
                wind_speed_mps=3.0,
                cloud_cover_pct=40.0,
                source="open_meteo",
            )
        ]

    monkeypatch.setattr(
        "weather.farm_views.get_farm_hourly_forecast", fake_hourly
    )

    resp = client.get(f"/api/v1/farms/{farm.id}/weather/hourly/")

    assert resp.status_code == status.HTTP_200_OK
    body = resp.json()
    assert body["status"] == 0
    assert len(body["data"]["hours"]) == 1


@pytest.mark.django_db
def test_farm_weather_hourly_invalid_params() -> None:
    farm = _create_farm()
    client = _auth_client()

    resp = client.get(
        f"/api/v1/farms/{farm.id}/weather/hourly/",
        {"hours": "999"},
    )

    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert resp.json()["status"] == 1


@pytest.mark.django_db
def test_farm_weather_hourly_missing_farm() -> None:
    client = _auth_client()

    resp = client.get("/api/v1/farms/99999/weather/hourly/")

    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["status"] == 1


@pytest.mark.django_db
def test_farm_weather_hourly_upstream_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    farm = _create_farm()
    client = _auth_client()

    async def fake_hourly(*, farm: Farm, hours: int) -> list[HourlyForecast]:
        raise WeatherUpstreamError()

    monkeypatch.setattr(
        "weather.farm_views.get_farm_hourly_forecast", fake_hourly
    )

    resp = client.get(f"/api/v1/farms/{farm.id}/weather/hourly/")

    assert resp.status_code == status.HTTP_502_BAD_GATEWAY
    assert resp.json()["status"] == 1


@pytest.mark.django_db
def test_farm_weather_daily_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    farm = _create_farm()
    client = _auth_client()

    async def fake_daily(*, farm: Farm, days: int) -> list[DailySummary]:
        assert days == 7
        return [
            DailySummary(
                day=datetime(2025, 1, 1, tzinfo=UTC).date(),
                t_min_c=15.0,
                t_max_c=25.0,
                precipitation_mm=1.2,
                wind_speed_max_mps=6.0,
                source="open_meteo",
            )
        ]

    monkeypatch.setattr(
        "weather.farm_views.get_farm_daily_summary", fake_daily
    )

    resp = client.get(f"/api/v1/farms/{farm.id}/weather/daily/")

    assert resp.status_code == status.HTTP_200_OK
    body = resp.json()
    assert body["status"] == 0
    assert len(body["data"]["forecasts"]) == 1


@pytest.mark.django_db
def test_farm_weather_daily_invalid_params() -> None:
    farm = _create_farm()
    client = _auth_client()

    resp = client.get(
        f"/api/v1/farms/{farm.id}/weather/daily/",
        {"days": "20"},
    )

    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert resp.json()["status"] == 1


@pytest.mark.django_db
def test_farm_weather_daily_missing_farm() -> None:
    client = _auth_client()

    resp = client.get("/api/v1/farms/99999/weather/daily/")

    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["status"] == 1


@pytest.mark.django_db
def test_farm_weather_daily_upstream_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    farm = _create_farm()
    client = _auth_client()

    async def fake_daily(*, farm: Farm, days: int) -> list[DailySummary]:
        raise WeatherUpstreamError()

    monkeypatch.setattr(
        "weather.farm_views.get_farm_daily_summary", fake_daily
    )

    resp = client.get(f"/api/v1/farms/{farm.id}/weather/daily/")

    assert resp.status_code == status.HTTP_502_BAD_GATEWAY
    assert resp.json()["status"] == 1
