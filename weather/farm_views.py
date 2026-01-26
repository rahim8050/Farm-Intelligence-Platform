"""Farm weather endpoints.

Authentication: integration JWT (Bearer token minted by
/api/v1/integrations/token/).
Responses: wrapped by config.api.responses.success_response.
"""

from __future__ import annotations

from typing import cast

from asgiref.sync import async_to_sync
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiTypes,
    extend_schema,
    inline_serializer,
)
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from config.api.openapi import (
    error_envelope_serializer,
    success_envelope_serializer,
)
from config.api.responses import JSONValue, success_response
from farms.models import Farm
from integrations.authentication import IntegrationJWTAuthentication

from .serializers import (
    CurrentWeatherSerializer,
    DailySummarySerializer,
    FarmDailyParamsSerializer,
    FarmHourlyParamsSerializer,
    HourlyForecastSerializer,
    serialize_current,
    serialize_daily_summary,
    serialize_hourly,
)
from .services import (
    get_farm_current_weather,
    get_farm_daily_summary,
    get_farm_hourly_forecast,
)

farm_weather_error_schema = error_envelope_serializer(
    "FarmWeatherErrorResponse"
)

current_success_schema = success_envelope_serializer(
    "FarmWeatherCurrentSuccess",
    data=CurrentWeatherSerializer(),
)

hourly_success_schema = success_envelope_serializer(
    "FarmWeatherHourlySuccess",
    data=inline_serializer(
        name="FarmWeatherHourlyData",
        fields={"hours": HourlyForecastSerializer(many=True)},
    ),
)

daily_success_schema = success_envelope_serializer(
    "FarmWeatherDailySuccess",
    data=inline_serializer(
        name="FarmWeatherDailyData",
        fields={"forecasts": DailySummarySerializer(many=True)},
    ),
)


class BaseFarmWeatherView(APIView):
    """Shared helpers for farm weather endpoints.

    Auth: IntegrationJWTAuthentication.
    Permissions: IsAuthenticated.
    Response envelope: success_response.
    """

    authentication_classes = (IntegrationJWTAuthentication,)
    permission_classes = (IsAuthenticated,)

    def _get_farm(self, farm_id: int) -> Farm:
        return get_object_or_404(Farm, id=farm_id, is_active=True)


@extend_schema(auth=cast(list[str], [{"BearerAuth": []}]))
class FarmWeatherCurrentView(BaseFarmWeatherView):
    """Fetch current weather for a farm.

    Response: success envelope with current observation fields.
    """

    @extend_schema(
        operation_id="v1_farms_weather_current_retrieve",
        responses={
            200: current_success_schema,
            400: farm_weather_error_schema,
            401: farm_weather_error_schema,
            403: farm_weather_error_schema,
            404: farm_weather_error_schema,
            502: farm_weather_error_schema,
        },
    )
    def get(self, request: Request, farm_id: int) -> Response:
        """Return current conditions for the farm centroid or bbox center."""

        if request.query_params:
            unknown = [
                key
                for key in request.query_params.keys()
                if key not in {"format"}
            ]
            if unknown:
                raise ValidationError("Unexpected query parameters.")

        farm = self._get_farm(farm_id)
        current = async_to_sync(get_farm_current_weather)(farm=farm)
        return success_response(serialize_current(current))


@extend_schema(auth=cast(list[str], [{"BearerAuth": []}]))
class FarmWeatherHourlyView(BaseFarmWeatherView):
    """Fetch hourly forecasts for a farm.

    Response: success envelope with hourly entries for the next N hours.
    """

    @extend_schema(
        operation_id="v1_farms_weather_hourly_retrieve",
        parameters=[
            OpenApiParameter(
                name="hours",
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                required=False,
                description="Number of hours ahead (1-168, default 48)",
            ),
        ],
        responses={
            200: hourly_success_schema,
            400: farm_weather_error_schema,
            401: farm_weather_error_schema,
            403: farm_weather_error_schema,
            404: farm_weather_error_schema,
            502: farm_weather_error_schema,
        },
    )
    def get(self, request: Request, farm_id: int) -> Response:
        """Return hourly forecasts for the requested window."""

        farm = self._get_farm(farm_id)
        serializer = FarmHourlyParamsSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        hours = int(serializer.validated_data.get("hours", 48))
        forecasts = async_to_sync(get_farm_hourly_forecast)(
            farm=farm, hours=hours
        )
        payload = {"hours": cast(JSONValue, serialize_hourly(forecasts))}
        return success_response(payload)


@extend_schema(auth=cast(list[str], [{"BearerAuth": []}]))
class FarmWeatherDailyView(BaseFarmWeatherView):
    """Fetch daily summaries for a farm.

    Response: success envelope with daily forecast summaries.
    """

    @extend_schema(
        operation_id="v1_farms_weather_daily_retrieve",
        parameters=[
            OpenApiParameter(
                name="days",
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                required=False,
                description="Number of days ahead (1-14, default 7)",
            ),
        ],
        responses={
            200: daily_success_schema,
            400: farm_weather_error_schema,
            401: farm_weather_error_schema,
            403: farm_weather_error_schema,
            404: farm_weather_error_schema,
            502: farm_weather_error_schema,
        },
    )
    def get(self, request: Request, farm_id: int) -> Response:
        """Return daily summaries for the requested window."""

        farm = self._get_farm(farm_id)
        serializer = FarmDailyParamsSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        days = int(serializer.validated_data.get("days", 7))
        summaries = async_to_sync(get_farm_daily_summary)(farm=farm, days=days)
        payload = {
            "forecasts": cast(JSONValue, serialize_daily_summary(summaries))
        }
        return success_response(payload)
