"""Radio API endpoints.

This module provides endpoints for radio station discovery, streaming,
and a lightweight health probe.

Auth: Public access by default. Authenticated endpoints (favorites
and listening history) require ``IsAuthenticated`` and use the global
JWT and API-key authentication classes.
Response: All responses use success_response envelope. Errors follow
the standard DRF error envelope.
"""

from __future__ import annotations

from typing import Any, cast

from django.contrib.auth.models import AbstractBaseUser, AnonymousUser
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from config.api.openapi import (
    error_envelope_serializer,
    success_envelope_serializer,
)
from config.api.responses import success_response
from radio.models import Provider, Station
from radio.serializers import (
    FavoriteCreateSerializer,
    FavoriteSerializer,
    ListeningHistorySerializer,
    ProviderSerializer,
    StationDetailSerializer,
    StationSerializer,
)
from radio.services import (
    add_favorite,
    list_favorites_for_user,
    list_history_for_user,
    record_listening_session,
    remove_favorite,
    summarize_health,
)

StationListEnvelope = inline_serializer(
    name="StationListEnvelope",
    fields={
        "status": serializers.IntegerField(),
        "message": serializers.CharField(),
        "data": StationSerializer(many=True),
        "errors": serializers.JSONField(allow_null=True),
    },
)

StationDetailEnvelope = inline_serializer(
    name="StationDetailEnvelope",
    fields={
        "status": serializers.IntegerField(),
        "message": serializers.CharField(),
        "data": StationDetailSerializer(),
        "errors": serializers.JSONField(allow_null=True),
    },
)

ProviderListEnvelope = inline_serializer(
    name="ProviderListEnvelope",
    fields={
        "status": serializers.IntegerField(),
        "message": serializers.CharField(),
        "data": ProviderSerializer(many=True),
        "errors": serializers.JSONField(allow_null=True),
    },
)

StreamUrlEnvelope = inline_serializer(
    name="StreamUrlEnvelope",
    fields={
        "status": serializers.IntegerField(),
        "message": serializers.CharField(),
        "data": inline_serializer(
            name="StreamUrlData",
            fields={
                "stream_url": serializers.URLField(),
                "format": serializers.CharField(),
                "bitrate": serializers.IntegerField(),
                "station_name": serializers.CharField(),
            },
        ),
        "errors": serializers.JSONField(allow_null=True),
    },
)

RadioHealthData = inline_serializer(
    name="RadioHealthData",
    fields={
        "status": serializers.CharField(
            help_text='"healthy", "degraded", or "unhealthy"'
        ),
        "timestamp": serializers.DateTimeField(),
        "stations_total": serializers.IntegerField(),
        "stations_available": serializers.IntegerField(),
        "stations_unavailable": serializers.IntegerField(),
        "stations_unchecked": serializers.IntegerField(),
    },
)

RadioHealthEnvelope = inline_serializer(
    name="RadioHealthEnvelope",
    fields={
        "status": serializers.IntegerField(),
        "message": serializers.CharField(),
        "data": RadioHealthData,
        "errors": serializers.JSONField(allow_null=True),
    },
)

FavoriteEnvelope = success_envelope_serializer(
    "RadioFavoriteEnvelope", data=FavoriteSerializer()
)
FavoriteListEnvelope = success_envelope_serializer(
    "RadioFavoriteListEnvelope", data=FavoriteSerializer(many=True)
)
FavoriteCreateEnvelope = success_envelope_serializer(
    "RadioFavoriteCreateEnvelope", data=FavoriteSerializer()
)
ListeningHistoryListEnvelope = success_envelope_serializer(
    "RadioListeningHistoryListEnvelope",
    data=ListeningHistorySerializer(many=True),
)
radio_error_envelope = error_envelope_serializer("RadioErrorResponse")
radio_null_envelope = success_envelope_serializer(
    "RadioNullEnvelope", data=serializers.JSONField(allow_null=True)
)


class StationListView(APIView):
    """List all available radio stations.

    Auth: Public
    Throttle: None (public endpoint)
    Response: envelope with `data` = list of StationSerializer.
    """

    permission_classes = [AllowAny]

    @extend_schema(
        responses={200: StationListEnvelope},
        summary="List radio stations",
        description="Returns all active radio stations.",
        operation_id="v1_radio_stations_list",
    )
    def get(self, request: Request) -> Response:
        """Get all active radio stations.

        Outputs:
            - status: 0
            - message: "Stations retrieved successfully"
            - data: list of station objects
        """
        stations = Station.objects.filter(is_active=True).select_related(
            "provider"
        )
        return success_response(
            StationSerializer(stations, many=True).data,
            message="Stations retrieved successfully",
        )


class StationDetailView(APIView):
    """Get single station details.

    Auth: Public
    Throttle: None
    Response: envelope with `data` = StationDetailSerializer.
    """

    permission_classes = [AllowAny]

    @extend_schema(
        responses={200: StationDetailEnvelope},
        summary="Get station details",
        description="Returns details for a specific radio station.",
        operation_id="v1_radio_stations_retrieve",
    )
    def get(self, request: Request, station_id: str) -> Response:
        """Get station by ID.

        Args:
            station_id: The station identifier

        Outputs:
            - status: 0
            - message: "Station retrieved successfully"
            - data: station details including stream URL
        """
        station = (
            Station.objects.filter(
                id=station_id,
                is_active=True,
            )
            .select_related("provider")
            .first()
        )

        if not station:
            raise NotFound("Station not found")

        return success_response(
            StationDetailSerializer(station).data,
            message="Station retrieved successfully",
        )


class StationStreamView(APIView):
    """Get stream URL for playback.

    The station must be active and currently available (i.e. the
    most recent health check was successful). Unavailable stations
    return HTTP 503 with an explanatory error so clients can fall
    back to an alternative or surface a "station down" UI state.

    Auth: Public
    Throttle: None
    Response: envelope with `data` containing stream_url and format.
    Errors:
        - 404 if the station does not exist or is inactive
        - 503 if the station is not currently available
    """

    permission_classes = [AllowAny]

    @extend_schema(
        responses={200: StreamUrlEnvelope},
        summary="Get stream URL",
        description=(
            "Returns the stream URL and metadata for playback. "
            "Returns 503 if the station's most recent health check "
            "marked it unavailable."
        ),
        operation_id="v1_radio_stations_stream",
    )
    def get(self, request: Request, station_id: str) -> Response:
        """Get stream URL for a station.

        Args:
            station_id: The station identifier

        Outputs:
            - status: 0
            - message: "Stream URL retrieved successfully"
            - data: stream_url, format, bitrate, station_name

        Side effects:
            - None (read-only).
        """
        station = (
            Station.objects.filter(
                id=station_id,
                is_active=True,
            )
            .select_related("provider")
            .first()
        )

        if not station:
            raise NotFound("Station not found")

        if station.is_available is False:
            return Response(
                {
                    "status": 1,
                    "message": "Station is currently unavailable",
                    "data": None,
                    "errors": {
                        "station_id": station.id,
                        "reason": "health_check_failed",
                    },
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        user = cast(
            AbstractBaseUser | AnonymousUser,
            getattr(request, "user", AnonymousUser()),
        )
        record_listening_session(user, station, request=request)

        return success_response(
            {
                "stream_url": station.stream_url,
                "format": station.format,
                "bitrate": station.bitrate,
                "station_name": station.name,
            },
            message="Stream URL retrieved successfully",
        )


class RadioHealthView(APIView):
    """Health probe for the radio service.

    Returns a snapshot of station availability derived from the most
    recent health-check pass. The endpoint is intended for ops
    dashboards and uptime monitoring; it does not require auth.

    Auth: Public
    Throttle: None
    Response: envelope with `data` = RadioHealthData fields.
    """

    permission_classes = [AllowAny]

    @extend_schema(
        responses={200: RadioHealthEnvelope},
        summary="Radio service health",
        description=(
            "Returns aggregate station availability: total active "
            "stations, available, unavailable, and unchecked counts, "
            "with a status flag of 'healthy', 'degraded', or "
            "'unhealthy'."
        ),
        operation_id="v1_radio_health",
    )
    def get(self, request: Request) -> Response:
        """Return a lightweight health snapshot for the radio service.

        Outputs:
            - data.status: one of 'healthy', 'degraded', 'unhealthy'
            - data.stations_total / available / unavailable / unchecked
            - data.timestamp: server time at which the snapshot was built
        """
        data = summarize_health()
        return success_response(data, message="Radio health OK")


class ProviderListView(APIView):
    """List all radio providers.

    Auth: Public
    Throttle: None
    Response: envelope with `data` = list of ProviderSerializer.
    """

    permission_classes = [AllowAny]

    @extend_schema(
        responses={200: ProviderListEnvelope},
        summary="List providers",
        description="Returns all active radio providers.",
        operation_id="v1_radio_providers_list",
    )
    def get(self, request: Request) -> Response:
        """Get all active providers.

        Outputs:
            - status: 0
            - message: "Providers retrieved successfully"
            - data: list of provider objects
        """
        providers = Provider.objects.filter(is_active=True)
        return success_response(
            ProviderSerializer(providers, many=True).data,
            message="Providers retrieved successfully",
        )


# ---------------------------------------------------------------------------
# Phase 3: Favorites and listening history (auth required)
# ---------------------------------------------------------------------------


@extend_schema(
    auth=cast(list[str], [{"BearerAuth": []}, {"ApiKeyAuth": []}]),
)
class FavoriteListCreateView(APIView):
    """List or add the current user's favorite stations.

    Authentication: BearerAuth (JWT) or ApiKeyAuth.
    Permissions: IsAuthenticated.
    Throttling: scope ``radio_favorites`` (60/min).
    Request body (POST): ``FavoriteCreateSerializer``
    (``{"station_id": "..."}``).
    Response data: ``FavoriteSerializer`` (list on GET, single on POST).
    Side effects: ``POST`` idempotently creates a favorite row.
    """

    permission_classes = [IsAuthenticated]
    throttle_scope = "radio_favorites"

    @extend_schema(
        responses={
            200: FavoriteListEnvelope,
            401: radio_error_envelope,
        },
        summary="List favorite stations",
        description=(
            "Returns the current user's favorite stations, newest first."
        ),
        operation_id="v1_radio_favorites_list",
    )
    def get(self, request: Request) -> Response:
        """Return the user's favorites, newest first.

        Side effects: none.
        """
        user = cast(
            AbstractBaseUser | AnonymousUser,
            getattr(request, "user", AnonymousUser()),
        )
        favorites = list_favorites_for_user(user, limit=100)
        data = FavoriteSerializer(favorites, many=True).data
        return success_response(data, message="Favorites retrieved")

    @extend_schema(
        request=FavoriteCreateSerializer,
        responses={
            201: FavoriteCreateEnvelope,
            400: radio_error_envelope,
            401: radio_error_envelope,
            404: radio_error_envelope,
        },
        summary="Add a favorite station",
        description=(
            "Idempotent: re-favoriting the same station returns the "
            "existing row. 404 if the station id is unknown or inactive."
        ),
        operation_id="v1_radio_favorites_create",
    )
    def post(self, request: Request) -> Response:
        """Add a station to the user's favorites (idempotent).

        Side effects: creates a ``Favorite`` row when missing.
        """
        serializer = FavoriteCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        station = Station.objects.get(
            id=serializer.validated_data["station_id"],
            is_active=True,
        )
        user = cast(
            AbstractBaseUser | AnonymousUser,
            getattr(request, "user", AnonymousUser()),
        )
        favorite, created = add_favorite(user, station)
        status_code = (
            status.HTTP_201_CREATED if created else status.HTTP_200_OK
        )
        return success_response(
            FavoriteSerializer(favorite).data,
            message=("Favorite added" if created else "Already a favorite"),
            status_code=status_code,
        )


@extend_schema(
    auth=cast(list[str], [{"BearerAuth": []}, {"ApiKeyAuth": []}]),
)
class FavoriteDeleteView(APIView):
    """Remove a station from the current user's favorites.

    Authentication: BearerAuth (JWT) or ApiKeyAuth.
    Permissions: IsAuthenticated.
    Throttling: scope ``radio_favorites`` (60/min).
    Side effects: deletes the matching favorite row (idempotent — 204
    even when no row existed).
    """

    permission_classes = [IsAuthenticated]
    throttle_scope = "radio_favorites"

    @extend_schema(
        responses={
            204: radio_null_envelope,
            401: radio_error_envelope,
        },
        summary="Remove a favorite station",
        description=(
            "Idempotent: returns 204 whether or not the row existed."
        ),
        operation_id="v1_radio_favorites_delete",
    )
    def delete(self, request: Request, station_id: str) -> Response:
        """Remove the favorite matching (user, station_id).

        Side effects: deletes the matching ``Favorite`` row when it
        exists.
        """
        user = cast(
            AbstractBaseUser | AnonymousUser,
            getattr(request, "user", AnonymousUser()),
        )
        remove_favorite(user, station_id)
        return success_response(None, message="Favorite removed")


@extend_schema(
    auth=cast(list[str], [{"BearerAuth": []}, {"ApiKeyAuth": []}]),
)
class ListeningHistoryListView(APIView):
    """List the current user's listening history.

    Authentication: BearerAuth (JWT) or ApiKeyAuth.
    Permissions: IsAuthenticated.
    Throttling: scope ``radio_history`` (60/min).
    Response data: list of ``ListeningHistorySerializer``, newest first,
    capped at 100 rows.
    Side effects: none.
    """

    permission_classes = [IsAuthenticated]
    throttle_scope = "radio_history"

    @extend_schema(
        responses={
            200: ListeningHistoryListEnvelope,
            401: radio_error_envelope,
        },
        summary="List listening history",
        description=(
            "Returns the most recent 100 listening-history rows for "
            "the current user, newest first."
        ),
        operation_id="v1_radio_history_list",
    )
    def get(self, request: Request) -> Response:
        """Return the user's listening history, newest first.

        Side effects: none.
        """
        user = cast(
            AbstractBaseUser | AnonymousUser,
            getattr(request, "user", AnonymousUser()),
        )
        rows = list_history_for_user(user, limit=100)
        data: list[Any] = ListeningHistorySerializer(rows, many=True).data
        return success_response(data, message="History retrieved")


@extend_schema(
    auth=cast(list[str], [{"BearerAuth": []}, {"ApiKeyAuth": []}]),
)
class ListeningHistoryRecentView(APIView):
    """Return the most recent N listening-history events.

    Authentication: BearerAuth (JWT) or ApiKeyAuth.
    Permissions: IsAuthenticated.
    Throttling: scope ``radio_history`` (60/min).
    Query params: ``limit`` (int, 1..100, default 20).
    Response data: list of ``ListeningHistorySerializer``, newest first.
    Side effects: none.
    """

    permission_classes = [IsAuthenticated]
    throttle_scope = "radio_history"

    @extend_schema(
        responses={
            200: ListeningHistoryListEnvelope,
            400: radio_error_envelope,
            401: radio_error_envelope,
        },
        summary="Recent listening history",
        description=(
            "Returns the most recent ``limit`` listening-history rows "
            "for the current user (default 20, max 100)."
        ),
        operation_id="v1_radio_history_recent",
    )
    def get(self, request: Request) -> Response:
        """Return the user's most recent history rows.

        Side effects: none.
        """
        try:
            limit_raw = request.query_params.get("limit", "20")
            limit = int(limit_raw)
        except (TypeError, ValueError):
            return success_response(
                data=None,
                message="Invalid limit",
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        if limit < 1 or limit > 100:
            return success_response(
                data=None,
                message="limit must be between 1 and 100",
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        user = cast(
            AbstractBaseUser | AnonymousUser,
            getattr(request, "user", AnonymousUser()),
        )
        rows = list_history_for_user(user, limit=limit)
        data: list[Any] = ListeningHistorySerializer(rows, many=True).data
        return success_response(data, message="Recent history retrieved")
