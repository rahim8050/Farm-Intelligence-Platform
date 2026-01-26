from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, cast
from zoneinfo import ZoneInfo

import httpx
from django.conf import settings
from django.utils import timezone as dj_timezone
from rest_framework.exceptions import APIException

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

logger = logging.getLogger(__name__)


class NasaPowerUpstreamError(APIException):
    status_code = 502
    default_detail = "NASA POWER upstream error"
    default_code = "nasa_power_upstream_error"


class NasaPowerProvider(WeatherProvider):
    """NASA POWER daily point provider.

    The API returns daily aggregates only; `current` returns the most recent
    daily value from a small local-day window (today and yesterday).
    """

    name: ProviderName = "nasa_power"
    _PARAMS = "T2M_MIN,T2M_MAX,PRECTOTCORR"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float = 10.0,
        community: str | None = None,
    ) -> None:
        self.base_url: str = base_url or cast(
            str,
            getattr(
                settings,
                "NASA_POWER_BASE_URL",
                "https://power.larc.nasa.gov/api/temporal/daily/point",
            ),
        )
        self.timeout = timeout
        self.community = (
            community
            or getattr(settings, "WEATHER_NASA_POWER_COMMUNITY", None)
            or os.getenv("WEATHER_NASA_POWER_COMMUNITY")
            or "AG"
        )

    async def current(self, loc: Location) -> CurrentWeather:
        zone = get_zone(loc.tz)
        today = dj_timezone.localtime(dj_timezone.now(), zone).date()
        start = today - timedelta(days=1)
        forecasts = await self.daily(loc, start, today)
        latest = max(forecasts, key=lambda f: f.day, default=None)

        observed_day = latest.day if latest else today
        observed_at = ensure_aware(
            datetime.combine(observed_day, time.min), zone
        )

        temperature = self._choose_temperature(latest)
        return CurrentWeather(
            observed_at=observed_at,
            temperature_c=temperature,
            wind_speed_mps=None,
            source=self.name,
        )

    async def daily(
        self, loc: Location, start: date, end: date
    ) -> Sequence[DailyForecast]:
        zone = get_zone(loc.tz)
        params = {
            "latitude": loc.lat,
            "longitude": loc.lon,
            "start": self._format_yyyymmdd(start),
            "end": self._format_yyyymmdd(end),
            "time-standard": "UTC",
            "community": self.community,
            "parameters": self._PARAMS,
            "format": "JSON",
        }
        response = await self._request(params)
        properties = (
            response.get("properties", {})
            if isinstance(response, dict)
            else {}
        )
        fill_value = properties.get("fill_value", -999)
        parameters = (
            properties.get("parameter", {})
            if isinstance(properties, dict)
            else {}
        )

        tmin_data = parameters.get("T2M_MIN") or {}
        tmax_data = parameters.get("T2M_MAX") or {}
        precip_data = parameters.get("PRECTOTCORR") or {}

        day_keys: set[str] = set()
        for container in (tmin_data, tmax_data, precip_data):
            if isinstance(container, dict):
                day_keys.update(container.keys())

        forecasts: list[DailyForecast] = []
        for key in sorted(day_keys):
            local_day = self._parse_day_to_local(key, zone)
            if local_day is None:
                continue
            t_min = self._extract_value(tmin_data, key, fill_value)
            t_max = self._extract_value(tmax_data, key, fill_value)
            precipitation = self._extract_value(precip_data, key, fill_value)
            forecasts.append(
                DailyForecast(
                    day=local_day,
                    t_min_c=t_min,
                    t_max_c=t_max,
                    precipitation_mm=precipitation,
                    source=self.name,
                )
            )
        return forecasts

    async def daily_summary(
        self, loc: Location, start: date, end: date
    ) -> Sequence[DailySummary]:
        forecasts = await self.daily(loc, start, end)
        return [
            DailySummary(
                day=forecast.day,
                t_min_c=forecast.t_min_c,
                t_max_c=forecast.t_max_c,
                precipitation_mm=forecast.precipitation_mm,
                wind_speed_max_mps=None,
                source=self.name,
            )
            for forecast in forecasts
        ]

    async def hourly(
        self, loc: Location, hours: int
    ) -> Sequence[HourlyForecast]:
        raise NotImplementedError(
            "NASA POWER does not provide hourly forecasts."
        )

    async def _request(self, params: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(self.base_url, params=params)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status_code = getattr(exc.response, "status_code", None)
            body_snippet = self._response_snippet(exc.response)
            logger.warning(
                "NASA POWER upstream error: status=%s body=%s",
                status_code,
                body_snippet or "<empty>",
            )
            raise NasaPowerUpstreamError() from exc
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Unexpected NASA POWER response shape")
        return data

    def _parse_day_to_local(self, raw: str, zone: ZoneInfo) -> date | None:
        try:
            utc_day = datetime.strptime(raw, "%Y%m%d").replace(
                tzinfo=timezone.utc  # noqa: UP017
            )
        except ValueError:
            return None
        return utc_day.astimezone(zone).date()

    def _extract_value(
        self, container: Any, key: str, fill_value: Any
    ) -> float | None:
        if not isinstance(container, dict):
            return None
        raw = container.get(key)
        if raw is None:
            return None
        try:
            if raw == fill_value or float(raw) == float(fill_value):
                return None
        except (TypeError, ValueError):
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def _choose_temperature(
        self, latest: DailyForecast | None
    ) -> float | None:
        if latest is None:
            return None
        if latest.t_min_c is not None and latest.t_max_c is not None:
            return (latest.t_min_c + latest.t_max_c) / 2
        if latest.t_max_c is not None:
            return latest.t_max_c
        return latest.t_min_c

    def _format_yyyymmdd(self, value: date) -> str:
        return value.strftime("%Y%m%d")

    def _response_snippet(self, response: httpx.Response | None) -> str | None:
        if response is None:
            return None
        try:
            text = response.text.strip()
        except Exception:
            return None
        if not text:
            return None
        if len(text) > 200:
            return f"{text[:200]}..."
        return text
