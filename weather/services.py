from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TypedDict, cast

import httpx
from django.conf import settings
from django.core.cache import caches
from django.core.cache.backends.base import BaseCache
from django.utils import timezone as dj_timezone
from rest_framework.exceptions import APIException, ValidationError

from farms.models import Farm

from .engines.nasa_power import NasaPowerUpstreamError
from .engines.registry import build_registry, validate_provider
from .engines.types import (
    CurrentWeather,
    DailyForecast,
    DailySummary,
    HourlyForecast,
    Location,
    ProviderName,
    WeeklyReport,
)
from .metrics import (
    weather_cache_hits_total,
    weather_cache_misses_total,
    weather_provider_errors_total,
    weather_provider_latency_seconds,
    weather_provider_requests_total,
)
from .timeutils import get_zone

DEFAULT_TZ = getattr(settings, "WEATHER_DEFAULT_TZ", "Africa/Nairobi")
CACHE_TTL_CURRENT = int(getattr(settings, "WEATHER_CACHE_TTL_CURRENT_S", 120))
CACHE_TTL_DAILY = int(getattr(settings, "WEATHER_CACHE_TTL_DAILY_S", 900))
CACHE_TTL_WEEKLY = int(getattr(settings, "WEATHER_CACHE_TTL_WEEKLY_S", 1800))
CACHE_LOCK_TIMEOUT = int(getattr(settings, "WEATHER_CACHE_LOCK_TIMEOUT_S", 5))
CACHE_LOCK_WAIT_SECONDS = float(
    getattr(settings, "WEATHER_CACHE_LOCK_WAIT_SECONDS", CACHE_LOCK_TIMEOUT)
)
CACHE_STALE_TTL_CURRENT = int(
    getattr(
        settings,
        "WEATHER_CACHE_STALE_TTL_CURRENT_S",
        max(300, CACHE_TTL_CURRENT * 3),
    )
)
MAX_RANGE_DAYS = int(getattr(settings, "WEATHER_MAX_RANGE_DAYS", 366))
FARM_CACHE_TTL_CURRENT = 60
FARM_CACHE_TTL_HOURLY = 600
FARM_CACHE_TTL_DAILY = 1800

PROVIDER_REGISTRY = build_registry()


logger = logging.getLogger(__name__)


class WeatherUpstreamError(APIException):
    status_code = 502
    default_detail = "Weather upstream error"
    default_code = "weather_upstream_error"


@dataclass(frozen=True)
class CacheKey:
    endpoint: str
    provider: ProviderName
    lat: float
    lon: float
    tz: str
    start: date | None = None
    end: date | None = None

    def as_string(self) -> str:
        rounded_lat = f"{self.lat:.4f}"
        rounded_lon = f"{self.lon:.4f}"
        start_part = self.start.isoformat() if self.start else "-"
        end_part = self.end.isoformat() if self.end else "-"
        return (
            f"weather:{self.endpoint}:{self.provider}:"
            f"{rounded_lat}:{rounded_lon}:{self.tz}:"
            f"{start_part}:{end_part}"
        )


@dataclass(frozen=True)
class FarmCacheKey:
    endpoint: str
    provider: ProviderName
    farm_id: int
    lat: float
    lon: float
    tz: str
    hours: int | None = None
    days: int | None = None

    def as_string(self) -> str:
        rounded_lat = f"{self.lat:.4f}"
        rounded_lon = f"{self.lon:.4f}"
        hours_part = str(self.hours) if self.hours is not None else "-"
        days_part = str(self.days) if self.days is not None else "-"
        return (
            f"farm-weather:{self.endpoint}:{self.provider}:{self.farm_id}:"
            f"{rounded_lat}:{rounded_lon}:{self.tz}:{hours_part}:{days_part}"
        )


def _select_provider(name: str | None) -> ProviderName:
    try:
        return validate_provider(name, PROVIDER_REGISTRY)
    except ValueError as exc:
        raise ValidationError("Invalid weather provider.") from exc


def _resolve_farm_location(farm: Farm) -> Location:
    if farm.centroid_lat is not None and farm.centroid_lon is not None:
        lat = float(farm.centroid_lat)
        lon = float(farm.centroid_lon)
        return Location(lat=lat, lon=lon, tz=DEFAULT_TZ)

    if (
        farm.bbox_south is not None
        and farm.bbox_west is not None
        and farm.bbox_north is not None
        and farm.bbox_east is not None
    ):
        lat = float((farm.bbox_south + farm.bbox_north) / 2)
        lon = float((farm.bbox_west + farm.bbox_east) / 2)
        return Location(lat=lat, lon=lon, tz=DEFAULT_TZ)

    raise ValidationError(
        "Farm must have a centroid or bounding box for weather."
    )


def _stale_cache_key(key: str) -> str:
    return f"{key}:stale"


def _lock_cache_key(key: str) -> str:
    return f"{key}:lock"


async def _wait_for_cached_value(
    cache: BaseCache,
    key: str,
    stale_key: str,
    timeout: float,
) -> tuple[CurrentWeather | None, bool]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        cached = cache.get(key)
        if cached:
            return cast(CurrentWeather, cached), False
        await asyncio.sleep(0.05)
    stale = cache.get(stale_key)
    if stale:
        return cast(CurrentWeather, stale), True
    return None, False


def _handle_upstream_error(exc: Exception) -> None:
    raise WeatherUpstreamError() from exc


async def get_current_weather(
    lat: float,
    lon: float,
    tz: str = DEFAULT_TZ,
    provider: str | None = None,
) -> CurrentWeather:
    get_zone(tz)
    provider_name = _select_provider(provider)
    key = CacheKey(
        endpoint="current",
        provider=provider_name,
        lat=lat,
        lon=lon,
        tz=tz,
    )
    cache = caches["default"]
    key_str = key.as_string()
    stale_key = _stale_cache_key(key_str)
    lock_key = _lock_cache_key(key_str)

    cached = cache.get(key_str)
    if cached:
        weather_cache_hits_total.labels(
            provider=provider_name, endpoint="current"
        ).inc()
        return cast(CurrentWeather, cached)
    lock_acquired = cache.add(
        lock_key,
        1,
        timeout=max(1, CACHE_LOCK_TIMEOUT),
    )
    if not lock_acquired:
        cached_value, _ = await _wait_for_cached_value(
            cache, key_str, stale_key, CACHE_LOCK_WAIT_SECONDS
        )
        if cached_value:
            weather_cache_hits_total.labels(
                provider=provider_name, endpoint="current"
            ).inc()
            return cached_value
        raise WeatherUpstreamError(
            "Weather cache refresh unavailable while another request "
            "is fetching."
        )

    weather_cache_misses_total.labels(
        provider=provider_name, endpoint="current"
    ).inc()
    provider_impl = PROVIDER_REGISTRY[provider_name]
    location = Location(lat=lat, lon=lon, tz=tz)

    start_time = time.perf_counter()
    weather_provider_requests_total.labels(
        provider=provider_name, endpoint="current"
    ).inc()
    try:
        result = await provider_impl.current(location)
    except Exception as exc:
        weather_provider_errors_total.labels(
            provider=provider_name,
            endpoint="current",
            error_type=exc.__class__.__name__,
        ).inc()
        stale_value = cache.get(stale_key)
        if stale_value is not None:
            logger.warning(
                "Returning stale current weather after upstream failure",
                exc_info=exc,
            )
            return cast(CurrentWeather, stale_value)
        raise
    finally:
        duration = time.perf_counter() - start_time
        weather_provider_latency_seconds.labels(
            provider=provider_name, endpoint="current"
        ).observe(duration)
        cache.delete(lock_key)

    cache.set(key_str, result, CACHE_TTL_CURRENT)
    cache.set(stale_key, result, CACHE_STALE_TTL_CURRENT)
    return result


async def get_farm_current_weather(
    farm: Farm, provider: str | None = None
) -> CurrentWeather:
    location = _resolve_farm_location(farm)
    provider_name = _select_provider(provider)
    key = FarmCacheKey(
        endpoint="current",
        provider=provider_name,
        farm_id=farm.id,
        lat=location.lat,
        lon=location.lon,
        tz=location.tz,
    )
    cache = caches["default"]
    cached = cache.get(key.as_string())
    if cached:
        weather_cache_hits_total.labels(
            provider=provider_name, endpoint="farm_current"
        ).inc()
        return cached

    weather_cache_misses_total.labels(
        provider=provider_name, endpoint="farm_current"
    ).inc()
    provider_impl = PROVIDER_REGISTRY[provider_name]
    start_time = time.perf_counter()
    weather_provider_requests_total.labels(
        provider=provider_name, endpoint="farm_current"
    ).inc()
    try:
        result = await provider_impl.current(location)
    except (
        httpx.HTTPError,
        NasaPowerUpstreamError,
        NotImplementedError,
        ValueError,
    ) as exc:
        weather_provider_errors_total.labels(
            provider=provider_name,
            endpoint="farm_current",
            error_type=exc.__class__.__name__,
        ).inc()
        _handle_upstream_error(exc)
    finally:
        duration = time.perf_counter() - start_time
        weather_provider_latency_seconds.labels(
            provider=provider_name, endpoint="farm_current"
        ).observe(duration)

    cache.set(key.as_string(), result, FARM_CACHE_TTL_CURRENT)
    return result


async def get_farm_hourly_forecast(
    farm: Farm, hours: int, provider: str | None = None
) -> Sequence[HourlyForecast]:
    location = _resolve_farm_location(farm)
    provider_name = _select_provider(provider)
    key = FarmCacheKey(
        endpoint="hourly",
        provider=provider_name,
        farm_id=farm.id,
        lat=location.lat,
        lon=location.lon,
        tz=location.tz,
        hours=hours,
    )
    cache = caches["default"]
    cached = cache.get(key.as_string())
    if cached:
        weather_cache_hits_total.labels(
            provider=provider_name, endpoint="farm_hourly"
        ).inc()
        return cached

    weather_cache_misses_total.labels(
        provider=provider_name, endpoint="farm_hourly"
    ).inc()
    provider_impl = PROVIDER_REGISTRY[provider_name]
    start_time = time.perf_counter()
    weather_provider_requests_total.labels(
        provider=provider_name, endpoint="farm_hourly"
    ).inc()
    try:
        result = await provider_impl.hourly(location, hours)
    except (
        httpx.HTTPError,
        NasaPowerUpstreamError,
        NotImplementedError,
        ValueError,
    ) as exc:
        weather_provider_errors_total.labels(
            provider=provider_name,
            endpoint="farm_hourly",
            error_type=exc.__class__.__name__,
        ).inc()
        _handle_upstream_error(exc)
    finally:
        duration = time.perf_counter() - start_time
        weather_provider_latency_seconds.labels(
            provider=provider_name, endpoint="farm_hourly"
        ).observe(duration)

    cache.set(key.as_string(), result, FARM_CACHE_TTL_HOURLY)
    return result


async def get_farm_daily_summary(
    farm: Farm, days: int, provider: str | None = None
) -> Sequence[DailySummary]:
    location = _resolve_farm_location(farm)
    provider_name = _select_provider(provider)
    key = FarmCacheKey(
        endpoint="daily",
        provider=provider_name,
        farm_id=farm.id,
        lat=location.lat,
        lon=location.lon,
        tz=location.tz,
        days=days,
    )
    cache = caches["default"]
    cached = cache.get(key.as_string())
    if cached:
        weather_cache_hits_total.labels(
            provider=provider_name, endpoint="farm_daily"
        ).inc()
        return cached

    weather_cache_misses_total.labels(
        provider=provider_name, endpoint="farm_daily"
    ).inc()
    provider_impl = PROVIDER_REGISTRY[provider_name]
    zone = get_zone(location.tz)
    today = dj_timezone.localtime(dj_timezone.now(), zone).date()
    end = today + timedelta(days=days - 1)

    start_time = time.perf_counter()
    weather_provider_requests_total.labels(
        provider=provider_name, endpoint="farm_daily"
    ).inc()
    try:
        result = await provider_impl.daily_summary(location, today, end)
    except (
        httpx.HTTPError,
        NasaPowerUpstreamError,
        NotImplementedError,
        ValueError,
    ) as exc:
        weather_provider_errors_total.labels(
            provider=provider_name,
            endpoint="farm_daily",
            error_type=exc.__class__.__name__,
        ).inc()
        _handle_upstream_error(exc)
    finally:
        duration = time.perf_counter() - start_time
        weather_provider_latency_seconds.labels(
            provider=provider_name, endpoint="farm_daily"
        ).observe(duration)

    cache.set(key.as_string(), result, FARM_CACHE_TTL_DAILY)
    return result


async def get_daily_forecast(
    lat: float,
    lon: float,
    start: date,
    end: date,
    tz: str = DEFAULT_TZ,
    provider: str | None = None,
) -> Sequence[DailyForecast]:
    return await _fetch_daily_forecasts(
        lat=lat,
        lon=lon,
        start=start,
        end=end,
        tz=tz,
        provider=provider,
        endpoint_label="daily",
    )


async def _fetch_daily_forecasts(
    *,
    lat: float,
    lon: float,
    start: date,
    end: date,
    tz: str,
    provider: str | None,
    endpoint_label: str,
) -> Sequence[DailyForecast]:
    if start > end:
        raise ValidationError("start must be on or before end.")
    if (end - start) > timedelta(days=MAX_RANGE_DAYS):
        raise ValidationError("Requested range exceeds the allowed window.")

    provider_name = _select_provider(provider)
    get_zone(tz)  # validate tz
    key = CacheKey(
        endpoint="daily",
        provider=provider_name,
        lat=lat,
        lon=lon,
        tz=tz,
        start=start,
        end=end,
    )
    cache = caches["default"]
    cache_key = key.as_string()
    cached = cache.get(cache_key)
    if cached:
        weather_cache_hits_total.labels(
            provider=provider_name, endpoint=endpoint_label
        ).inc()
        return cached

    weather_cache_misses_total.labels(
        provider=provider_name, endpoint=endpoint_label
    ).inc()
    provider_impl = PROVIDER_REGISTRY[provider_name]
    location = Location(lat=lat, lon=lon, tz=tz)

    start_time = time.perf_counter()
    weather_provider_requests_total.labels(
        provider=provider_name, endpoint=endpoint_label
    ).inc()
    try:
        result = await provider_impl.daily(location, start, end)
    except Exception as exc:
        weather_provider_errors_total.labels(
            provider=provider_name,
            endpoint=endpoint_label,
            error_type=exc.__class__.__name__,
        ).inc()
        raise
    finally:
        duration = time.perf_counter() - start_time
        weather_provider_latency_seconds.labels(
            provider=provider_name, endpoint=endpoint_label
        ).observe(duration)

    cache.set(cache_key, result, CACHE_TTL_DAILY)
    return result


async def get_weekly_report(
    lat: float,
    lon: float,
    start: date,
    end: date,
    tz: str = DEFAULT_TZ,
    provider: str | None = None,
) -> Sequence[WeeklyReport]:
    provider_name = _select_provider(provider)
    get_zone(tz)
    key = CacheKey(
        endpoint="weekly",
        provider=provider_name,
        lat=lat,
        lon=lon,
        tz=tz,
        start=start,
        end=end,
    )
    cache = caches["default"]
    cache_key = key.as_string()
    cached = cache.get(cache_key)
    if cached:
        weather_cache_hits_total.labels(
            provider=provider_name, endpoint="weekly"
        ).inc()
        return cached

    weather_cache_misses_total.labels(
        provider=provider_name, endpoint="weekly"
    ).inc()
    daily_forecasts = await _fetch_daily_forecasts(
        lat=lat,
        lon=lon,
        start=start,
        end=end,
        tz=tz,
        provider=provider_name,
        endpoint_label="weekly",
    )
    weekly = _aggregate_weekly(daily_forecasts, provider_name)
    cache.set(cache_key, weekly, CACHE_TTL_WEEKLY)
    return weekly


class WeeklyBucket(TypedDict):
    week_end: date
    days: list[DailyForecast]
    tmin_sum: float
    tmin_count: int
    tmax_sum: float
    tmax_count: int
    precip_sum: float
    precip_count: int


def _aggregate_weekly(
    forecasts: Sequence[DailyForecast], provider: ProviderName
) -> list[WeeklyReport]:
    buckets: dict[date, WeeklyBucket] = {}
    for forecast in sorted(forecasts, key=lambda f: f.day):
        week_start = forecast.day - timedelta(days=forecast.day.weekday())
        week_end = week_start + timedelta(days=6)
        bucket = buckets.setdefault(
            week_start,
            {
                "week_end": week_end,
                "days": [],
                "tmin_sum": 0.0,
                "tmin_count": 0,
                "tmax_sum": 0.0,
                "tmax_count": 0,
                "precip_sum": 0.0,
                "precip_count": 0,
            },
        )
        bucket["days"].append(forecast)

        if forecast.t_min_c is not None:
            bucket["tmin_sum"] = float(bucket["tmin_sum"]) + float(
                forecast.t_min_c
            )
            bucket["tmin_count"] = int(bucket["tmin_count"]) + 1
        if forecast.t_max_c is not None:
            bucket["tmax_sum"] = float(bucket["tmax_sum"]) + float(
                forecast.t_max_c
            )
            bucket["tmax_count"] = int(bucket["tmax_count"]) + 1
        if forecast.precipitation_mm is not None:
            bucket["precip_sum"] = float(bucket["precip_sum"]) + float(
                forecast.precipitation_mm
            )
            bucket["precip_count"] = int(bucket["precip_count"]) + 1

    reports: list[WeeklyReport] = []
    for week_start, bucket in sorted(buckets.items()):
        tmin_avg = (
            bucket["tmin_sum"] / bucket["tmin_count"]
            if bucket["tmin_count"]
            else None
        )
        tmax_avg = (
            bucket["tmax_sum"] / bucket["tmax_count"]
            if bucket["tmax_count"]
            else None
        )
        precip_sum = bucket["precip_sum"] if bucket["precip_count"] else None
        reports.append(
            WeeklyReport(
                week_start=week_start,
                week_end=bucket["week_end"],
                t_min_avg_c=tmin_avg,
                t_max_avg_c=tmax_avg,
                precipitation_sum_mm=precip_sum,
                days=bucket["days"],
                source=provider,
            )
        )
    return reports
