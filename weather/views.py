"""Weather API endpoints.

Authentication: default JWT or API key from REST_FRAMEWORK settings.
Responses: wrapped by `config.api.responses.success_response`
(status/message/data/errors).
"""

from __future__ import annotations

from typing import cast

from asgiref.sync import async_to_sync
from django.conf import settings
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiTypes,
    extend_schema,
    inline_serializer,
)
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from config.api.openapi import (
    error_envelope_serializer,
    success_envelope_serializer,
)
from config.api.proxy import proxy_json_request
from config.api.responses import JSONValue, success_response

from .serializers import (
    BaseWeatherParamsSerializer,
    CurrentWeatherSerializer,
    DailyForecastSerializer,
    RangeWeatherParamsSerializer,
    WeeklyReportSerializer,
    serialize_current,
    serialize_daily,
    serialize_weekly,
)
from .services import (
    DEFAULT_TZ,
    get_current_weather,
    get_daily_forecast,
    get_weekly_report,
)

current_success_schema = success_envelope_serializer(
    "WeatherCurrentSuccess",
    data=CurrentWeatherSerializer(),
)
weather_error_schema = error_envelope_serializer("WeatherErrorResponse")

daily_success_schema = success_envelope_serializer(
    "WeatherDailySuccess",
    data=inline_serializer(
        name="WeatherDailyData",
        fields={"forecasts": DailyForecastSerializer(many=True)},
    ),
)

weekly_success_schema = success_envelope_serializer(
    "WeatherWeeklySuccess",
    data=inline_serializer(
        name="WeatherWeeklyData",
        fields={"reports": WeeklyReportSerializer(many=True)},
    ),
)


def _provider_cache_token(provider: str | None) -> str:
    return provider or "default"


class WeatherCurrentView(APIView):
    """Fetch current weather for a location.

    Auth: IsAuthenticated (JWT or API key).
    Response: success envelope with `observed_at`, `temperature_c`,
    `wind_speed_mps`, and provider `source`. When proxying is enabled,
    responses are forwarded from the weather microservice.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="lat",
                type=OpenApiTypes.FLOAT,
                location=OpenApiParameter.QUERY,
                required=True,
            ),
            OpenApiParameter(
                name="lon",
                type=OpenApiTypes.FLOAT,
                location=OpenApiParameter.QUERY,
                required=True,
            ),
            OpenApiParameter(
                name="tz",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                required=False,
                description="IANA timezone (default Africa/Nairobi)",
            ),
            OpenApiParameter(
                name="provider",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                required=False,
                description="Weather provider (open_meteo or nasa_power)",
            ),
        ],
        responses={
            200: current_success_schema,
            400: weather_error_schema,
            401: weather_error_schema,
            403: weather_error_schema,
        },
    )
    def get(self, request: Request) -> Response:
        """Return current conditions.

        Inputs: lat/lon (required), optional tz/provider.
        Outputs: envelope with the current observation timestamp (+offset),
        temperature (C), wind speed (m/s), provider name.
        """

        serializer = BaseWeatherParamsSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        params = serializer.validated_data
        lat = float(params["lat"])
        lon = float(params["lon"])
        tz = str(params.get("tz") or DEFAULT_TZ)
        provider = cast(str | None, params.get("provider"))
        if settings.WEATHER_PROXY_ENABLED:
            return proxy_json_request(
                request,
                settings.WEATHER_SERVICE_URL,
                "/api/v1/weather/current/",
                params={
                    "lat": f"{lat}",
                    "lon": f"{lon}",
                    "tz": tz,
                    "provider": provider or "",
                },
                cache_key=(
                    f"weather-proxy:current:{_provider_cache_token(provider)}:"
                    f"{lat:.4f}:{lon:.4f}:{tz}"
                ),
                cache_ttl_s=int(
                    getattr(settings, "WEATHER_CACHE_TTL_CURRENT_S", 120)
                ),
            )

        current = async_to_sync(get_current_weather)(
            lat=lat,
            lon=lon,
            tz=tz,
            provider=provider,
        )
        return success_response(serialize_current(current))


class WeatherDailyView(APIView):
    """Fetch daily forecasts/observations over a date range.

    Auth: IsAuthenticated (JWT or API key).
    Response: success envelope with a `forecasts` list of daily values
    in the requested timezone. When proxying is enabled, responses are
    forwarded from the weather microservice.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="lat",
                type=OpenApiTypes.FLOAT,
                location=OpenApiParameter.QUERY,
                required=True,
            ),
            OpenApiParameter(
                name="lon",
                type=OpenApiTypes.FLOAT,
                location=OpenApiParameter.QUERY,
                required=True,
            ),
            OpenApiParameter(
                name="start",
                type=OpenApiTypes.DATE,
                location=OpenApiParameter.QUERY,
                required=True,
            ),
            OpenApiParameter(
                name="end",
                type=OpenApiTypes.DATE,
                location=OpenApiParameter.QUERY,
                required=True,
            ),
            OpenApiParameter(
                name="tz",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                required=False,
                description="IANA timezone (default Africa/Nairobi)",
            ),
            OpenApiParameter(
                name="provider",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                required=False,
                description="Weather provider (open_meteo or nasa_power)",
            ),
        ],
        responses={
            200: daily_success_schema,
            400: weather_error_schema,
            401: weather_error_schema,
            403: weather_error_schema,
        },
    )
    def get(self, request: Request) -> Response:
        """Return daily data for the inclusive date range.

        Inputs: lat, lon, start/end dates (YYYY-MM-DD), optional tz/provider.
        Outputs: envelope with `forecasts` containing daily
        min/max/precipitation in Africa/Nairobi by default.
        """

        serializer = RangeWeatherParamsSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        params = serializer.validated_data
        lat = float(params["lat"])
        lon = float(params["lon"])
        tz = str(params.get("tz") or DEFAULT_TZ)
        provider = cast(str | None, params.get("provider"))
        start = params["start"]
        end = params["end"]
        if settings.WEATHER_PROXY_ENABLED:
            return proxy_json_request(
                request,
                settings.WEATHER_SERVICE_URL,
                "/api/v1/weather/daily/",
                params={
                    "lat": f"{lat}",
                    "lon": f"{lon}",
                    "tz": tz,
                    "provider": provider or "",
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                },
                cache_key=(
                    f"weather-proxy:daily:{_provider_cache_token(provider)}:"
                    f"{lat:.4f}:{lon:.4f}:{tz}:{start.isoformat()}:{end.isoformat()}"
                ),
                cache_ttl_s=int(
                    getattr(settings, "WEATHER_CACHE_TTL_DAILY_S", 900)
                ),
            )

        forecasts = async_to_sync(get_daily_forecast)(
            lat=lat,
            lon=lon,
            start=start,
            end=end,
            tz=tz,
            provider=provider,
        )
        forecast_payload = serialize_daily(forecasts)
        return success_response(
            {"forecasts": cast(JSONValue, forecast_payload)}
        )


class WeatherWeeklyView(APIView):
    """Fetch weekly aggregates (Monday-Sunday) derived from daily data.

    Auth: IsAuthenticated (JWT or API key).
    Response: success envelope with `reports` list, each entry covering one
    calendar week in the requested timezone. When proxying is enabled,
    responses are forwarded from the weather microservice.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="lat",
                type=OpenApiTypes.FLOAT,
                location=OpenApiParameter.QUERY,
                required=True,
            ),
            OpenApiParameter(
                name="lon",
                type=OpenApiTypes.FLOAT,
                location=OpenApiParameter.QUERY,
                required=True,
            ),
            OpenApiParameter(
                name="start",
                type=OpenApiTypes.DATE,
                location=OpenApiParameter.QUERY,
                required=True,
            ),
            OpenApiParameter(
                name="end",
                type=OpenApiTypes.DATE,
                location=OpenApiParameter.QUERY,
                required=True,
            ),
            OpenApiParameter(
                name="tz",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                required=False,
                description="IANA timezone (default Africa/Nairobi)",
            ),
            OpenApiParameter(
                name="provider",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                required=False,
                description="Weather provider (open_meteo or nasa_power)",
            ),
        ],
        responses={
            200: weekly_success_schema,
            400: weather_error_schema,
            401: weather_error_schema,
            403: weather_error_schema,
        },
    )
    def get(self, request: Request) -> Response:
        """Return weekly aggregates over the supplied date range.

        Inputs: lat, lon, start/end dates (YYYY-MM-DD), optional tz/provider.
        Outputs: envelope with `reports` where each report contains weekly
        averages (temps) and summed precipitation in Africa/Nairobi by default.
        """

        serializer = RangeWeatherParamsSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        params = serializer.validated_data
        lat = float(params["lat"])
        lon = float(params["lon"])
        tz = str(params.get("tz") or DEFAULT_TZ)
        provider = cast(str | None, params.get("provider"))
        start = params["start"]
        end = params["end"]
        if settings.WEATHER_PROXY_ENABLED:
            return proxy_json_request(
                request,
                settings.WEATHER_SERVICE_URL,
                "/api/v1/weather/weekly/",
                params={
                    "lat": f"{lat}",
                    "lon": f"{lon}",
                    "tz": tz,
                    "provider": provider or "",
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                },
                cache_key=(
                    f"weather-proxy:weekly:{_provider_cache_token(provider)}:"
                    f"{lat:.4f}:{lon:.4f}:{tz}:{start.isoformat()}:{end.isoformat()}"
                ),
                cache_ttl_s=int(
                    getattr(settings, "WEATHER_CACHE_TTL_WEEKLY_S", 1800)
                ),
            )

        reports = async_to_sync(get_weekly_report)(
            lat=lat,
            lon=lon,
            start=start,
            end=end,
            tz=tz,
            provider=provider,
        )
        reports_payload = serialize_weekly(reports)
        return success_response({"reports": cast(JSONValue, reports_payload)})
