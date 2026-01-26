from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta
from typing import ClassVar

from django.conf import settings
from django.utils import timezone as dj_timezone
from rest_framework import serializers

from config.api.responses import JSONValue

from .timeutils import get_zone, isoformat_with_tz

DEFAULT_TZ = getattr(settings, "WEATHER_DEFAULT_TZ", "Africa/Nairobi")
MAX_RANGE_DAYS = int(getattr(settings, "WEATHER_MAX_RANGE_DAYS", 366))
_DAILY_REQUIRED_FIELDS = ("t_min_c", "t_max_c", "precipitation_mm")


def _nasa_power_daily_lag_days() -> int:
    try:
        lag_days = int(getattr(settings, "NASA_POWER_DAILY_LAG_DAYS", 2))
    except (TypeError, ValueError):
        lag_days = 2
    return max(lag_days, 0)


def _nasa_power_cutoff_date() -> date:
    return dj_timezone.localdate() - timedelta(
        days=_nasa_power_daily_lag_days()
    )


def _missing_fields(obj: object) -> list[str]:
    missing: list[str] = []
    for field in _DAILY_REQUIRED_FIELDS:
        if getattr(obj, field, None) is None:
            missing.append(field)
    return missing


def _is_missing_day(obj: object) -> bool:
    return all(
        getattr(obj, field, None) is None for field in _DAILY_REQUIRED_FIELDS
    )


def _is_recent_nasa_gap(obj: object, missing: list[str]) -> bool:
    if not missing:
        return False
    if getattr(obj, "source", None) != "nasa_power":
        return False
    day = getattr(obj, "day", None)
    if not isinstance(day, date):
        return False
    return day > _nasa_power_cutoff_date()


def _is_partial_day(obj: object) -> bool:
    missing = _missing_fields(obj)
    return bool(missing) or _is_recent_nasa_gap(obj, missing)


class BaseWeatherParamsSerializer(serializers.Serializer):
    lat: ClassVar[serializers.FloatField] = serializers.FloatField(
        min_value=-90.0, max_value=90.0
    )
    lon: ClassVar[serializers.FloatField] = serializers.FloatField(
        min_value=-180.0, max_value=180.0
    )
    tz: ClassVar[serializers.CharField] = serializers.CharField(
        required=False, default=DEFAULT_TZ
    )
    provider: ClassVar[serializers.CharField] = serializers.CharField(
        required=False, allow_null=True
    )

    def _allowed_providers(self) -> Iterable[str]:
        return ("open_meteo", "nasa_power")

    def validate_tz(self, value: str) -> str:
        try:
            get_zone(value)
        except ValueError as exc:
            raise serializers.ValidationError("Invalid timezone.") from exc
        return value

    def validate_provider(self, value: str | None) -> str | None:
        if value is None:
            return None
        if value == "":
            return None
        normalized = value.lower()
        if normalized not in self._allowed_providers():
            raise serializers.ValidationError("Unknown provider.")
        return normalized


class RangeWeatherParamsSerializer(BaseWeatherParamsSerializer):
    start: ClassVar[serializers.DateField] = serializers.DateField()
    end: ClassVar[serializers.DateField] = serializers.DateField()

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        attrs = super().validate(attrs)
        start = attrs.get("start")
        end = attrs.get("end")
        if isinstance(start, date) and isinstance(end, date):
            if start > end:
                raise serializers.ValidationError(
                    "start must be on or before end."
                )
            delta_days = (end - start).days
            if delta_days > MAX_RANGE_DAYS:
                raise serializers.ValidationError(
                    "Requested range exceeds WEATHER_MAX_RANGE_DAYS."
                )
        return attrs


class FarmHourlyParamsSerializer(serializers.Serializer):
    hours: ClassVar[serializers.IntegerField] = serializers.IntegerField(
        required=False, min_value=1, max_value=168, default=48
    )


class FarmDailyParamsSerializer(serializers.Serializer):
    days: ClassVar[serializers.IntegerField] = serializers.IntegerField(
        required=False, min_value=1, max_value=14, default=7
    )


class CurrentWeatherSerializer(serializers.Serializer):
    observed_at: ClassVar[serializers.DateTimeField] = (
        serializers.DateTimeField()
    )
    temperature_c: ClassVar[serializers.FloatField] = serializers.FloatField(
        allow_null=True
    )
    wind_speed_mps: ClassVar[serializers.FloatField] = serializers.FloatField(
        allow_null=True
    )
    source: ClassVar[serializers.CharField] = serializers.CharField()  # type: ignore[misc,assignment]


class DailyForecastSerializer(serializers.Serializer):
    day: ClassVar[serializers.DateField] = serializers.DateField()
    t_min_c: ClassVar[serializers.FloatField] = serializers.FloatField(
        allow_null=True
    )
    t_max_c: ClassVar[serializers.FloatField] = serializers.FloatField(
        allow_null=True
    )
    precipitation_mm: ClassVar[serializers.FloatField] = (
        serializers.FloatField(allow_null=True)
    )
    is_partial: ClassVar[serializers.SerializerMethodField] = (
        serializers.SerializerMethodField()
    )
    missing_fields: ClassVar[serializers.SerializerMethodField] = (
        serializers.SerializerMethodField()
    )
    source: ClassVar[serializers.CharField] = serializers.CharField()  # type: ignore[misc,assignment]

    def get_is_partial(self, obj: object) -> bool:
        return _is_partial_day(obj)

    def get_missing_fields(self, obj: object) -> list[str]:
        return _missing_fields(obj)


class HourlyForecastSerializer(serializers.Serializer):
    timestamp: ClassVar[serializers.DateTimeField] = (
        serializers.DateTimeField()
    )
    temperature_c: ClassVar[serializers.FloatField] = serializers.FloatField(
        allow_null=True
    )
    precipitation_mm: ClassVar[serializers.FloatField] = (
        serializers.FloatField(allow_null=True)
    )
    wind_speed_mps: ClassVar[serializers.FloatField] = serializers.FloatField(
        allow_null=True
    )
    cloud_cover_pct: ClassVar[serializers.FloatField] = serializers.FloatField(
        allow_null=True
    )
    source: ClassVar[serializers.CharField] = serializers.CharField()  # type: ignore[misc,assignment]


class DailySummarySerializer(serializers.Serializer):
    day: ClassVar[serializers.DateField] = serializers.DateField()
    t_min_c: ClassVar[serializers.FloatField] = serializers.FloatField(
        allow_null=True
    )
    t_max_c: ClassVar[serializers.FloatField] = serializers.FloatField(
        allow_null=True
    )
    precipitation_mm: ClassVar[serializers.FloatField] = (
        serializers.FloatField(allow_null=True)
    )
    wind_speed_max_mps: ClassVar[serializers.FloatField] = (
        serializers.FloatField(allow_null=True)
    )
    source: ClassVar[serializers.CharField] = serializers.CharField()  # type: ignore[misc,assignment]


class WeeklyReportSerializer(serializers.Serializer):
    week_start: ClassVar[serializers.DateField] = serializers.DateField()
    week_end: ClassVar[serializers.DateField] = serializers.DateField()
    t_min_avg_c: ClassVar[serializers.FloatField] = serializers.FloatField(
        allow_null=True
    )
    t_max_avg_c: ClassVar[serializers.FloatField] = serializers.FloatField(
        allow_null=True
    )
    precipitation_sum_mm: ClassVar[serializers.FloatField] = (
        serializers.FloatField(allow_null=True)
    )
    days: ClassVar[DailyForecastSerializer] = DailyForecastSerializer(
        many=True
    )
    is_partial: ClassVar[serializers.SerializerMethodField] = (
        serializers.SerializerMethodField()
    )
    missing_days_count: ClassVar[serializers.SerializerMethodField] = (
        serializers.SerializerMethodField()
    )
    source: ClassVar[serializers.CharField] = serializers.CharField()  # type: ignore[misc,assignment]

    def get_is_partial(self, obj: object) -> bool:
        days = getattr(obj, "days", None) or []
        return any(_is_partial_day(day) for day in days)

    def get_missing_days_count(self, obj: object) -> int:
        days = getattr(obj, "days", None) or []
        return sum(1 for day in days if _is_missing_day(day))


def serialize_current(payload: object) -> dict[str, JSONValue]:
    serializer = CurrentWeatherSerializer(payload)
    data = serializer.data
    observed_attr = getattr(payload, "observed_at", None)
    if isinstance(observed_attr, datetime):
        data["observed_at"] = isoformat_with_tz(observed_attr)
    return data


def serialize_hourly(
    forecasts: Sequence[object],
) -> list[dict[str, JSONValue]]:
    serializer = HourlyForecastSerializer(forecasts, many=True)
    data = list(serializer.data)
    for idx, forecast in enumerate(forecasts):
        timestamp = getattr(forecast, "timestamp", None)
        if isinstance(timestamp, datetime):
            data[idx]["timestamp"] = isoformat_with_tz(timestamp)
    return data


def serialize_daily(
    forecasts: Sequence[object],
) -> list[dict[str, JSONValue]]:
    serializer = DailyForecastSerializer(forecasts, many=True)
    return list(serializer.data)


def serialize_weekly(
    reports: Sequence[object],
) -> list[dict[str, JSONValue]]:
    serializer = WeeklyReportSerializer(reports, many=True)
    return list(serializer.data)


def serialize_daily_summary(
    summaries: Sequence[object],
) -> list[dict[str, JSONValue]]:
    serializer = DailySummarySerializer(summaries, many=True)
    return list(serializer.data)
