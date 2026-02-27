from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

ProviderName = Literal["open_meteo", "nasa_power"]


@dataclass(frozen=True)
class Location:
    lat: float
    lon: float
    tz: str = "Africa/Nairobi"


@dataclass(frozen=True)
class CurrentWeather:
    observed_at: datetime
    temperature_c: float | None
    wind_speed_mps: float | None
    source: ProviderName


@dataclass(frozen=True)
class DailyForecast:
    day: date
    t_min_c: float | None
    t_max_c: float | None
    precipitation_mm: float | None
    source: ProviderName
    wind_speed_max_mps: float | None = None


@dataclass(frozen=True)
class WeeklyReport:
    week_start: date
    week_end: date
    t_min_avg_c: float | None
    t_max_avg_c: float | None
    precipitation_sum_mm: float | None
    days: Sequence[DailyForecast]
    source: ProviderName


@dataclass(frozen=True)
class HourlyForecast:
    timestamp: datetime
    temperature_c: float | None
    precipitation_mm: float | None
    wind_speed_mps: float | None
    cloud_cover_pct: float | None
    source: ProviderName


@dataclass(frozen=True)
class DailySummary:
    day: date
    t_min_c: float | None
    t_max_c: float | None
    precipitation_mm: float | None
    wind_speed_max_mps: float | None
    source: ProviderName
