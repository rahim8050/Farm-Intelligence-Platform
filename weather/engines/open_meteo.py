from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

import httpx
from django.conf import settings
from django.utils import timezone

from ..timeutils import ensure_aware, get_zone
from .base import WeatherProvider
from .types import (
    CurrentWeather,
    DailyForecast,
    DailySummary,
    HourlyForecast,
    Location,
    ProviderName,
)


class OpenMeteoProvider(WeatherProvider):
    """Open-Meteo implementation.

    Uses the `/v1/forecast` endpoint with timezone-aware parameters.
    """

    name: ProviderName = "open_meteo"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float = 10.0,
        max_retries: int = 2,
        backoff_seconds: float = 0.5,
    ) -> None:
        self.base_url: str = base_url or cast(
            str,
            getattr(
                settings,
                "OPEN_METEO_BASE_URL",
                "https://api.open-meteo.com/v1/forecast",
            ),
        )
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds

    async def current(self, loc: Location) -> CurrentWeather:
        zone = get_zone(loc.tz)
        params = {
            "latitude": loc.lat,
            "longitude": loc.lon,
            "current": "temperature_2m,wind_speed_10m",
            "timezone": loc.tz,
        }
        payload = await self._request(params)
        current_block = (
            payload.get("current", {}) if isinstance(payload, dict) else {}
        )

        observed_at = self._parse_datetime(current_block.get("time"), zone)
        if observed_at is None:
            observed_at = ensure_aware(timezone.now(), zone)

        temperature = self._to_float(current_block.get("temperature_2m"))
        wind_speed = self._to_float(current_block.get("wind_speed_10m"))

        return CurrentWeather(
            observed_at=observed_at,
            temperature_c=temperature,
            wind_speed_mps=wind_speed,
            source=self.name,
        )

    async def daily(
        self, loc: Location, start: date, end: date
    ) -> Sequence[DailyForecast]:
        params = {
            "latitude": loc.lat,
            "longitude": loc.lon,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "daily": (
                "temperature_2m_min,"
                "temperature_2m_max,"
                "precipitation_sum,"
                "wind_speed_10m_max"
            ),
            "timezone": loc.tz,
        }
        payload = await self._request(params)
        daily_block = (
            payload.get("daily", {}) if isinstance(payload, dict) else {}
        )
        dates = daily_block.get("time") or []
        t_min_list = daily_block.get("temperature_2m_min") or []
        t_max_list = daily_block.get("temperature_2m_max") or []
        precip_list = daily_block.get("precipitation_sum") or []
        wind_list = daily_block.get("wind_speed_10m_max") or []

        forecasts: list[DailyForecast] = []
        for idx, raw_day in enumerate(dates):
            day = self._parse_date(raw_day)
            if day is None:
                continue
            t_min = self._list_value(t_min_list, idx)
            t_max = self._list_value(t_max_list, idx)
            precip = self._list_value(precip_list, idx)
            forecasts.append(
                DailyForecast(
                    day=day,
                    t_min_c=t_min,
                    t_max_c=t_max,
                    precipitation_mm=precip,
                    source=self.name,
                    wind_speed_max_mps=self._list_value(wind_list, idx),
                )
            )
        return forecasts

    async def daily_summary(
        self, loc: Location, start: date, end: date
    ) -> Sequence[DailySummary]:
        params = {
            "latitude": loc.lat,
            "longitude": loc.lon,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "daily": (
                "temperature_2m_min,temperature_2m_max,"
                "precipitation_sum,wind_speed_10m_max"
            ),
            "timezone": loc.tz,
        }
        payload = await self._request(params)
        daily_block = (
            payload.get("daily", {}) if isinstance(payload, dict) else {}
        )
        dates = daily_block.get("time") or []
        t_min_list = daily_block.get("temperature_2m_min") or []
        t_max_list = daily_block.get("temperature_2m_max") or []
        precip_list = daily_block.get("precipitation_sum") or []
        wind_list = daily_block.get("wind_speed_10m_max") or []

        summaries: list[DailySummary] = []
        for idx, raw_day in enumerate(dates):
            day = self._parse_date(raw_day)
            if day is None:
                continue
            summaries.append(
                DailySummary(
                    day=day,
                    t_min_c=self._list_value(t_min_list, idx),
                    t_max_c=self._list_value(t_max_list, idx),
                    precipitation_mm=self._list_value(precip_list, idx),
                    wind_speed_max_mps=self._list_value(wind_list, idx),
                    source=self.name,
                )
            )
        return summaries

    async def hourly(
        self, loc: Location, hours: int
    ) -> Sequence[HourlyForecast]:
        zone = get_zone(loc.tz)
        params = {
            "latitude": loc.lat,
            "longitude": loc.lon,
            "hourly": (
                "temperature_2m,precipitation,wind_speed_10m,cloudcover"
            ),
            "forecast_hours": hours,
            "timezone": loc.tz,
        }
        payload = await self._request(params)
        hourly_block = (
            payload.get("hourly", {}) if isinstance(payload, dict) else {}
        )
        times = hourly_block.get("time") or []
        temps = hourly_block.get("temperature_2m") or []
        precips = hourly_block.get("precipitation") or []
        winds = hourly_block.get("wind_speed_10m") or []
        clouds = (
            hourly_block.get("cloudcover")
            or hourly_block.get("cloud_cover")
            or []
        )

        forecasts: list[HourlyForecast] = []
        for idx, raw_time in enumerate(times):
            timestamp = self._parse_datetime(raw_time, zone)
            if timestamp is None:
                continue
            forecasts.append(
                HourlyForecast(
                    timestamp=timestamp,
                    temperature_c=self._list_value(temps, idx),
                    precipitation_mm=self._list_value(precips, idx),
                    wind_speed_mps=self._list_value(winds, idx),
                    cloud_cover_pct=self._list_value(clouds, idx),
                    source=self.name,
                )
            )
        return forecasts

    async def _request(self, params: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.get(self.base_url, params=params)
                if response.status_code >= 500 and attempt < self.max_retries:
                    await asyncio.sleep(self.backoff_seconds * (attempt + 1))
                    continue
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError("Unexpected Open-Meteo response shape")
                return data
            except Exception as exc:  # pragma: no cover - exercised in tests
                last_error = exc
                if attempt < self.max_retries:
                    await asyncio.sleep(self.backoff_seconds * (attempt + 1))
                    continue
                raise
        if last_error is None:  # pragma: no cover - safety net
            raise RuntimeError("Open-Meteo request failed without exception")
        raise last_error

    def _parse_datetime(self, raw: Any, zone: ZoneInfo) -> datetime | None:
        if not isinstance(raw, str):
            return None
        candidate = raw
        if candidate.endswith("Z"):
            candidate = candidate.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            return None
        return ensure_aware(parsed, zone)

    def _parse_date(self, raw: Any) -> date | None:
        if not isinstance(raw, str):
            return None
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None

    def _list_value(self, values: Sequence[Any], idx: int) -> float | None:
        if idx >= len(values):
            return None
        return self._to_float(values[idx])

    def _to_float(self, value: Any) -> float | None:
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None
