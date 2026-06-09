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

import datetime
from typing import cast

import jwt as pyjwt
from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, AnonymousUser
from django.utils import timezone
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from config.api.openapi import (
    error_envelope_serializer,
    success_envelope_serializer,
)
from config.api.pagination import (
    paginated_envelope_serializer,
    paginated_response,
    pagination_parameters,
)
from config.api.responses import success_response
from radio.metrics import timed
from radio.models import (
    EmergencyBroadcast,
    Favorite,
    ListeningHistory,
    Provider,
    Station,
    StationHealthCheck,
)
from radio.serializers import (
    EmergencyBroadcastCreateSerializer,
    EmergencyBroadcastSerializer,
    EmergencyBroadcastUpdateSerializer,
    FavoriteCreateSerializer,
    FavoriteSerializer,
    ListeningHistorySerializer,
    NowPlayingSerializer,
    ProviderSerializer,
    StationAnalyticsSerializer,
    StationDetailSerializer,
    StationSerializer,
    TTSSynthesizeRequestSerializer,
)
from radio.services import (
    add_favorite,
    create_emergency_broadcast,
    delete_emergency_broadcast,
    get_current_emergency,
    get_fallback_station,
    get_now_playing,
    get_station_analytics,
    list_active_stations_cached,
    list_emergency_history,
    record_listening_session,
    remove_favorite,
    stop_listening_session,
    summarize_health,
    update_emergency_broadcast,
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
FavoriteListEnvelope = paginated_envelope_serializer(
    "RadioFavoriteListEnvelope", item=FavoriteSerializer()
)
FavoriteCreateEnvelope = success_envelope_serializer(
    "RadioFavoriteCreateEnvelope", data=FavoriteSerializer()
)
ListeningHistoryListEnvelope = paginated_envelope_serializer(
    "RadioListeningHistoryListEnvelope",
    item=ListeningHistorySerializer(),
)
radio_error_envelope = error_envelope_serializer("RadioErrorResponse")
radio_null_envelope = success_envelope_serializer(
    "RadioNullEnvelope", data=serializers.JSONField(allow_null=True)
)
EmergencyBroadcastEnvelope = success_envelope_serializer(
    "RadioEmergencyBroadcastEnvelope",
    data=EmergencyBroadcastSerializer(),
)
EmergencyBroadcastListEnvelope = success_envelope_serializer(
    "RadioEmergencyBroadcastListEnvelope",
    data=EmergencyBroadcastSerializer(many=True),
)
TTSSynthesizeData = inline_serializer(
    name="RadioTTSSynthesizeData",
    fields={
        "mime_type": serializers.CharField(),
        "duration_ms": serializers.IntegerField(),
        "audio_base64": serializers.CharField(
            help_text="Base64-encoded audio bytes."
        ),
    },
)
TTSSynthesizeEnvelope = success_envelope_serializer(
    "RadioTTSSynthesizeEnvelope", data=TTSSynthesizeData
)
StationHealthCheckData = inline_serializer(
    name="RadioStationHealthCheckData",
    fields={
        "station_id": serializers.CharField(),
        "is_reachable": serializers.BooleanField(),
        "checked_at": serializers.DateTimeField(),
        "status_code": serializers.IntegerField(allow_null=True),
        "response_time_ms": serializers.IntegerField(allow_null=True),
        "error_message": serializers.CharField(allow_blank=True),
    },
)
StationHealthHistoryEnvelope = inline_serializer(
    name="RadioStationHealthHistoryEnvelope",
    fields={
        "status": serializers.IntegerField(),
        "message": serializers.CharField(),
        "data": serializers.ListField(
            child=StationHealthCheckData,
            help_text="List of health-check rows, newest first.",
        ),
        "errors": serializers.JSONField(allow_null=True),
        "request_id": serializers.CharField(allow_null=True),
    },
)
NowPlayingEnvelope = success_envelope_serializer(
    "RadioNowPlayingEnvelope", data=NowPlayingSerializer()
)
StationAnalyticsListEnvelope = success_envelope_serializer(
    "RadioStationAnalyticsListEnvelope",
    data=StationAnalyticsSerializer(many=True),
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
    @timed("stations.list")
    def get(self, request: Request) -> Response:
        """Get all active radio stations.

        The list is served from the per-process cache for
        ``STATION_LIST_CACHE_TTL_SECONDS`` (default 60s). The cache
        is bypassed when ``?genre=`` or ``?country=`` filters are
        passed, because the cache key only covers the unfiltered
        list.

        Query params:
            genre: Optional exact-match filter on ``Station.genre``.
            country: Optional exact-match filter on
                ``Station.country``.

        Outputs:
            - status: 0
            - message: "Stations retrieved successfully"
            - data: list of station objects
        """
        genre = request.query_params.get("genre")
        country = request.query_params.get("country")
        if genre or country:
            qs = Station.objects.filter(is_active=True).select_related(
                "provider"
            )
            if genre:
                qs = qs.filter(genre=genre)
            if country:
                qs = qs.filter(country=country)
            stations = list(qs.order_by("name"))
        else:
            stations = list_active_stations_cached()
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
    @timed("stations.detail")
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
    @timed("stations.stream")
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
            fallback = get_fallback_station(station.id)
            fallback_payload: dict[str, object] | None = None
            if fallback is not None:
                fallback_payload = {
                    "station_id": fallback.id,
                    "station_name": fallback.name,
                    "stream_url": fallback.stream_url,
                    "format": fallback.format,
                    "bitrate": fallback.bitrate,
                }
            return Response(
                {
                    "status": 1,
                    "message": "Station is currently unavailable",
                    "data": None,
                    "errors": {
                        "station_id": station.id,
                        "reason": "health_check_failed",
                        "fallback": fallback_payload,
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


SignedStreamData = inline_serializer(
    name="SignedStreamData",
    fields={
        "token": serializers.CharField(
            help_text="JWT signed stream-access token"
        ),
        "stream_url": serializers.URLField(
            help_text="Resolved stream URL for playback"
        ),
        "expires_at": serializers.DateTimeField(
            help_text="Token expiry timestamp (UTC)"
        ),
        "format": serializers.CharField(
            help_text="Audio format (e.g. MP3, AAC, HLS)"
        ),
        "bitrate": serializers.IntegerField(
            help_text="Stream bitrate in kbps"
        ),
        "station_name": serializers.CharField(
            help_text="Human-readable station name"
        ),
    },
)
SignedStreamEnvelope = success_envelope_serializer(
    "RadioSignedStreamEnvelope",
    data=SignedStreamData,
)


@extend_schema(
    auth=cast(list[str], [{"BearerAuth": []}, {"ApiKeyAuth": []}]),
)
class SignedStreamView(APIView):
    """Return a time-limited signed stream URL.

    Generates a JWT that encodes ``station_id``, ``user_id``, and
    ``exp`` (expiry). The token is signed with the project's
    ``SECRET_KEY`` using HS256 so callers or a downstream proxy
    can verify it without a separate DB round-trip.

    Auth: IsAuthenticated
    Throttle: None
    Response: envelope with ``data`` = ``{token, stream_url,
    expires_at, format, bitrate, station_name}``.
    Errors: 401 if unauthenticated, 404 if station not found/inactive.
    Side effects: records a listening-history row.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        responses={200: SignedStreamEnvelope, 401: radio_error_envelope},
        summary="Get signed stream URL",
        description=(
            "Returns a time-limited JWT and the stream URL for "
            "playback. The token is valid for "
            "``RADIO_SIGNED_STREAM_TTL_SECONDS`` (default 3600s = 1 "
            "hour). Records a listening-history row for the "
            "authenticated user."
        ),
        operation_id="v1_radio_stations_stream_signed",
    )
    @timed("stations.stream_signed")
    def get(self, request: Request, station_id: str) -> Response:
        """Return signed stream URL for an authenticated client.

        Args:
            station_id: The station identifier.

        Outputs:
            - status: 0
            - message: "Signed stream URL generated"
            - data: token, stream_url, expires_at, format,
              bitrate, station_name

        Side effects:
            - Records a ListeningHistory row for the user/station.
        """
        station = (
            Station.objects.filter(id=station_id, is_active=True)
            .select_related("provider")
            .first()
        )
        if not station:
            raise NotFound("Station not found")

        ttl = getattr(settings, "RADIO_SIGNED_STREAM_TTL_SECONDS", 3600)
        expires_at = timezone.now() + datetime.timedelta(seconds=ttl)
        user = cast(AbstractBaseUser, request.user)

        token = cast(
            str,
            pyjwt.encode(
                {
                    "station_id": station.id,
                    "user_id": str(user.id),
                    "exp": int(expires_at.timestamp()),
                    "iat": int(timezone.now().timestamp()),
                    "purpose": "stream_access",
                },
                settings.SECRET_KEY,
                algorithm="HS256",
            ),
        )

        record_listening_session(user, station, request=request)

        return success_response(
            {
                "token": token,
                "stream_url": station.stream_url,
                "expires_at": expires_at.isoformat(),
                "format": station.format,
                "bitrate": station.bitrate,
                "station_name": station.name,
            },
            message="Signed stream URL generated",
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
    @timed("health")
    def get(self, request: Request) -> Response:
        """Return a lightweight health snapshot for the radio service.

        Outputs:
            - data.status: one of 'healthy', 'degraded', 'unhealthy'
            - data.stations_total / available / unavailable / unchecked
            - data.timestamp: server time at which the snapshot was built
        """
        data = summarize_health()
        return success_response(data, message="Radio health OK")


class StationHealthHistoryView(APIView):
    """Return the most recent health-check rows for one station.

    The endpoint surfaces the audit trail behind
    ``Station.is_available``: clients can see whether a station has
    been flaky and what its most recent probe outcomes look like.

    Auth: Public
    Throttle: None
    Path params: ``station_id`` (str).
    Query params: ``limit`` (1..100, default 20).
    Response: envelope with ``data`` = list of health-check rows,
    newest first.

    Side effects: none.
    """

    permission_classes = [AllowAny]

    @extend_schema(
        responses={
            200: StationHealthHistoryEnvelope,
            404: radio_error_envelope,
        },
        summary="Per-station health-check history",
        description=(
            "Returns the most recent health-check rows for a single "
            "station, newest first. `limit` is 1..100, default 20. "
            "Returns 404 if the station does not exist."
        ),
        operation_id="v1_radio_station_health_history",
    )
    @timed("stations.health_history")
    def get(self, request: Request, station_id: str) -> Response:
        """Return the recent health-check audit trail for a station."""
        if not Station.objects.filter(id=station_id).exists():
            raise NotFound("Station not found")
        try:
            limit = int(request.query_params.get("limit", "20"))
        except (TypeError, ValueError):
            return success_response(
                data=None,
                message="limit must be an integer",
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        if limit < 1 or limit > 100:
            return success_response(
                data=None,
                message="limit must be between 1 and 100",
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        rows = list(
            StationHealthCheck.objects.filter(station_id=station_id).order_by(
                "-checked_at"
            )[:limit]
        )
        payload = [
            {
                "station_id": r.station_id,
                "is_reachable": r.is_reachable,
                "checked_at": r.checked_at,
                "status_code": r.status_code,
                "response_time_ms": r.response_time_ms,
                "error_message": r.error_message or "",
            }
            for r in rows
        ]
        return success_response(payload, message="Station health history")


class StationNowPlayingView(APIView):
    """Return the cached now-playing metadata for one station.

    The endpoint surfaces the row populated by
    ``radio.tasks.refresh_now_playing``. Returns ``data: null`` when
    the station has no ``metadata_url`` configured or no successful
    poll has happened yet.

    Auth: Public
    Throttle: None
    Path params: ``station_id`` (str).
    Response: envelope with ``data`` = :class:`NowPlayingSerializer`
    or ``null``.
    Side effects: none.
    """

    permission_classes = [AllowAny]

    @extend_schema(
        responses={200: NowPlayingEnvelope, 404: radio_error_envelope},
        summary="Per-station now-playing metadata",
        description=(
            "Returns the cached now-playing metadata for a single "
            "station, or `data: null` if the station has no "
            "`metadata_url` configured or no successful poll has "
            "occurred yet."
        ),
        operation_id="v1_radio_station_now_playing",
    )
    @timed("stations.now_playing")
    def get(self, request: Request, station_id: str) -> Response:
        """Return the cached now-playing row for a station."""
        if not Station.objects.filter(id=station_id).exists():
            raise NotFound("Station not found")
        row = get_now_playing(station_id)
        payload = NowPlayingSerializer(row).data if row is not None else None
        return success_response(payload, message="Station now playing")


class StationAnalyticsView(APIView):
    """Return per-day listen analytics for one station.

    The endpoint surfaces the daily aggregates built by
    ``radio.tasks.rollup_station_analytics``. Newest first.

    Auth: Public
    Throttle: None
    Path params: ``station_id`` (str).
    Query params: ``days`` (1..90, default 7).
    Response: envelope with ``data`` = list of
    :class:`StationAnalyticsSerializer`.
    Side effects: none.
    """

    permission_classes = [AllowAny]

    @extend_schema(
        responses={
            200: StationAnalyticsListEnvelope,
            404: radio_error_envelope,
        },
        summary="Per-station analytics",
        description=(
            "Returns up to `days` (1..90, default 7) of daily "
            "analytics rows for a station, newest first. A row "
            "contains `total_listens`, `total_duration_seconds`, "
            "and `unique_users`."
        ),
        operation_id="v1_radio_station_analytics",
    )
    @timed("stations.analytics")
    def get(self, request: Request, station_id: str) -> Response:
        """Return the recent analytics rows for a station."""
        if not Station.objects.filter(id=station_id).exists():
            raise NotFound("Station not found")
        try:
            days = int(request.query_params.get("days", "7"))
        except (TypeError, ValueError):
            return success_response(
                data=None,
                message="days must be an integer",
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        if days < 1 or days > 90:
            return success_response(
                data=None,
                message="days must be between 1 and 90",
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        rows = get_station_analytics(station_id, days=days)
        return success_response(
            StationAnalyticsSerializer(rows, many=True).data,
            message="Station analytics",
        )


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
    @timed("providers.list")
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
        parameters=pagination_parameters(),
        summary="List favorite stations",
        description=(
            "Returns the current user's favorite stations, newest first. "
            "Paginated; supports `page` and `page_size` query params "
            "(default 20, max 100)."
        ),
        operation_id="v1_radio_favorites_list",
    )
    @timed("favorites.list")
    def get(self, request: Request) -> Response:
        """Return the user's favorites, newest first.

        Pagination: ``page`` (1-based) and ``page_size`` (1..100,
        default 20). The response ``data`` is a DRF page dict with
        ``count``, ``next``, ``previous``, and ``results``.

        Side effects: none.
        """
        user = cast(
            AbstractBaseUser | AnonymousUser,
            getattr(request, "user", AnonymousUser()),
        )
        qs = Favorite.objects.filter(user=user).select_related(
            "station", "station__provider"
        )
        return paginated_response(
            qs,
            FavoriteSerializer,
            request,
            message="Favorites retrieved",
        )

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
    @timed("favorites.create")
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
    @timed("favorites.delete")
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
        parameters=pagination_parameters(),
        summary="List listening history",
        description=(
            "Returns the current user's listening history, newest "
            "first. Paginated; supports `page` and `page_size` query "
            "params (default 20, max 100)."
        ),
        operation_id="v1_radio_history_list",
    )
    @timed("history.list")
    def get(self, request: Request) -> Response:
        """Return the user's listening history, newest first.

        Pagination: ``page`` (1-based) and ``page_size`` (1..100,
        default 20). The response ``data`` is a DRF page dict with
        ``count``, ``next``, ``previous``, and ``results``.

        Side effects: none.
        """
        user = cast(
            AbstractBaseUser | AnonymousUser,
            getattr(request, "user", AnonymousUser()),
        )
        qs = ListeningHistory.objects.filter(user=user).select_related(
            "station", "station__provider"
        )
        return paginated_response(
            qs,
            ListeningHistorySerializer,
            request,
            message="History retrieved",
        )


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
            "Returns the most recent listening-history rows for the "
            "current user. Query param ``limit`` (1..100, default 20) "
            "caps the number of rows returned. Response ``data`` is a "
            "DRF page dict with ``count``, ``next``, ``previous``, "
            "and ``results`` (at most ``limit`` items)."
        ),
        operation_id="v1_radio_history_recent",
    )
    @timed("history.recent")
    def get(self, request: Request) -> Response:
        """Return the user's most recent history rows.

        ``limit`` is a hard cap (1..100) on the size of ``results``;
        pagination is intentionally not exposed on this endpoint so
        a client can fetch "the N most recent" without computing
        pages.

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
        qs = (
            ListeningHistory.objects.filter(user=user)
            .select_related("station", "station__provider")
            .order_by("-started_at")[:limit]
        )
        return paginated_response(
            qs,
            ListeningHistorySerializer,
            request,
            message="Recent history retrieved",
            page_size=limit,
            max_page_size=limit,
        )


@extend_schema(
    auth=cast(list[str], [{"BearerAuth": []}, {"ApiKeyAuth": []}]),
)
class ListeningSessionStopView(APIView):
    """Stop a listening session (set ``ended_at``).

    The session must belong to the authenticated user. Idempotent:
    calling again on an already-stopped session is a no-op (200).

    Auth: IsAuthenticated
    Throttle: scope ``radio_history`` (60/min)
    Response: envelope with ``data`` = ``{ended_at}``.
    Errors: 401 if unauthenticated, 404 if session not found or
    belongs to another user.
    Side effects: sets ``ListeningHistory.ended_at``.
    """

    permission_classes = [IsAuthenticated]
    throttle_scope = "radio_history"

    @extend_schema(
        request=None,
        responses={
            200: radio_null_envelope,
            401: radio_error_envelope,
            404: radio_error_envelope,
        },
        summary="Stop a listening session",
        description=(
            "Sets ``ended_at`` on the listening-history row identified "
            "by ``session_id``. The session must belong to the "
            "authenticated user. Idempotent."
        ),
        operation_id="v1_radio_history_stop",
    )
    @timed("history.stop")
    def post(self, request: Request, session_id: int) -> Response:
        """Stop a listening session.

        Args:
            session_id: The listening-history row ID.

        Outputs:
            - status: 0
            - message: "Listening session stopped"
            - data: None

        Side effects:
            - Sets ``ListeningHistory.ended_at`` to now.
        """
        user = cast(
            AbstractBaseUser | AnonymousUser,
            getattr(request, "user", AnonymousUser()),
        )
        session = stop_listening_session(user, session_id)
        if session is None:
            raise NotFound("Listening session not found")
        return success_response(
            data=None,
            message="Listening session stopped",
        )


# ---------------------------------------------------------------------------
# Phase 5 (P5): Emergency broadcasts
# ---------------------------------------------------------------------------


class EmergencyCurrentView(APIView):
    """Return the currently active emergency broadcast, if any.

    "Active" means ``is_active=True`` and the current time falls inside
    ``[starts_at, ends_at]``. The endpoint returns the highest-priority
    broadcast (critical > high > medium > low), then the most recently
    started. Returns ``data: null`` when no broadcast is active.

    Auth: Public
    Throttle: None
    Response: envelope with ``data`` = :class:`EmergencyBroadcastSerializer`
    or ``null``.
    """

    permission_classes = [AllowAny]

    @extend_schema(
        responses={200: EmergencyBroadcastEnvelope},
        summary="Current emergency broadcast",
        description=(
            "Returns the highest-priority active emergency broadcast, "
            "or `data: null` when no broadcast is currently active."
        ),
        operation_id="v1_radio_emergency_current",
    )
    @timed("emergency.current")
    def get(self, request: Request) -> Response:
        """Return the active emergency broadcast (or null)."""
        broadcast = get_current_emergency()
        payload = (
            EmergencyBroadcastSerializer(broadcast).data
            if broadcast is not None
            else None
        )
        return success_response(payload, message="Current emergency broadcast")


class EmergencyHistoryView(APIView):
    """Return a paginated history of emergency broadcasts, newest first.

    Auth: Public
    Throttle: None
    Query params: ``limit`` (1..200, default 50), ``offset`` (>=0,
    default 0).
    Response: envelope with ``data`` = list of
    :class:`EmergencyBroadcastSerializer`.
    """

    permission_classes = [AllowAny]

    @extend_schema(
        responses={200: EmergencyBroadcastListEnvelope},
        summary="Emergency broadcast history",
        description=(
            "Returns emergency broadcasts ordered by `starts_at` "
            "descending. Supports `limit` (1..200, default 50) and "
            "`offset` (>=0, default 0) query parameters."
        ),
        operation_id="v1_radio_emergency_history",
    )
    @timed("emergency.history")
    def get(self, request: Request) -> Response:
        """Return the emergency-broadcast history page."""
        try:
            limit = int(request.query_params.get("limit", "50"))
            offset = int(request.query_params.get("offset", "0"))
        except (TypeError, ValueError):
            return success_response(
                data=None,
                message="limit and offset must be integers",
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        if limit < 1 or limit > 200:
            return success_response(
                data=None,
                message="limit must be between 1 and 200",
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        if offset < 0:
            return success_response(
                data=None,
                message="offset must be >= 0",
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        rows = list_emergency_history(limit=limit, offset=offset)
        return success_response(
            EmergencyBroadcastSerializer(rows, many=True).data,
            message="Emergency history retrieved",
        )


@extend_schema(
    auth=cast(list[str], [{"BearerAuth": []}, {"ApiKeyAuth": []}]),
)
class EmergencyCreateView(APIView):
    """Create a new emergency broadcast (admin only).

    Auth: IsAdminUser
    Throttle: None
    Request body: :class:`EmergencyBroadcastCreateSerializer`
    Response data: :class:`EmergencyBroadcastSerializer`.
    Side effects: inserts a new ``EmergencyBroadcast`` row.
    """

    permission_classes = [IsAdminUser]

    @extend_schema(
        request=EmergencyBroadcastCreateSerializer,
        responses={
            201: EmergencyBroadcastEnvelope,
            400: radio_error_envelope,
            401: radio_error_envelope,
            403: radio_error_envelope,
        },
        summary="Create an emergency broadcast (admin)",
        description=(
            "Create a new emergency broadcast. The creator is recorded "
            "as `created_by` for audit."
        ),
        operation_id="v1_radio_emergency_create",
    )
    @timed("emergency.create")
    def post(self, request: Request) -> Response:
        """Validate input and create an ``EmergencyBroadcast`` row."""
        serializer = EmergencyBroadcastCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        broadcast = create_emergency_broadcast(
            created_by=getattr(request, "user", None),
            **serializer.validated_data,
        )
        return success_response(
            EmergencyBroadcastSerializer(broadcast).data,
            message="Emergency broadcast created",
            status_code=status.HTTP_201_CREATED,
        )


@extend_schema(
    auth=cast(list[str], [{"BearerAuth": []}, {"ApiKeyAuth": []}]),
)
class EmergencyDetailView(APIView):
    """Update or delete a single emergency broadcast (admin only).

    Auth: IsAdminUser
    Throttle: None
    PATCH: :class:`EmergencyBroadcastUpdateSerializer`
    DELETE: idempotent — 200 even if the row is already gone.
    Response data: :class:`EmergencyBroadcastSerializer` (on PATCH).
    Side effects: updates or deletes one ``EmergencyBroadcast`` row.
    """

    permission_classes = [IsAdminUser]

    def _get_object(self, pk: int) -> EmergencyBroadcast:
        broadcast = EmergencyBroadcast.objects.filter(pk=pk).first()
        if broadcast is None:
            raise NotFound("Emergency broadcast not found")
        return broadcast

    @extend_schema(
        request=EmergencyBroadcastUpdateSerializer,
        responses={
            200: EmergencyBroadcastEnvelope,
            400: radio_error_envelope,
            401: radio_error_envelope,
            403: radio_error_envelope,
            404: radio_error_envelope,
        },
        summary="Update an emergency broadcast (admin)",
        operation_id="v1_radio_emergency_update",
    )
    @timed("emergency.update")
    def patch(self, request: Request, pk: int) -> Response:
        """Apply a partial update to one ``EmergencyBroadcast`` row."""
        broadcast = self._get_object(pk)
        serializer = EmergencyBroadcastUpdateSerializer(
            broadcast, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        updated = update_emergency_broadcast(
            broadcast, fields=serializer.validated_data
        )
        return success_response(
            EmergencyBroadcastSerializer(updated).data,
            message="Emergency broadcast updated",
        )

    @extend_schema(
        responses={
            200: radio_null_envelope,
            401: radio_error_envelope,
            403: radio_error_envelope,
        },
        summary="Delete an emergency broadcast (admin)",
        description="Idempotent: returns 200 whether or not the row existed.",
        operation_id="v1_radio_emergency_delete",
    )
    @timed("emergency.delete")
    def delete(self, request: Request, pk: int) -> Response:
        """Delete one ``EmergencyBroadcast`` row. Idempotent."""
        broadcast = EmergencyBroadcast.objects.filter(pk=pk).first()
        if broadcast is not None:
            delete_emergency_broadcast(broadcast)
        return success_response(None, message="Emergency broadcast removed")


# ---------------------------------------------------------------------------
# Phase 5 (P5): TTS endpoint (thin radio-side wrapper)
# ---------------------------------------------------------------------------


@extend_schema(
    auth=cast(list[str], [{"BearerAuth": []}, {"ApiKeyAuth": []}]),
)
class TTSSynthesizeView(APIView):
    """Synthesise text into audio using the project's TTS service.

    The endpoint is a thin radio-side wrapper around
    ``alerts.tts.synthesize`` — the actual TTS engines, the circuit
    breaker, and the per-engine executor pool all live in the
    ``alerts`` app and are reused unchanged.

    Auth: IsAuthenticated
    Throttle: None
    Request body: :class:`TTSSynthesizeRequestSerializer`
    (``{"text": "...", "voice": "..."}``).
    Response data: ``{mime_type, duration_ms, audio_base64}`` — the
    audio bytes are base64-encoded so they fit the JSON envelope.
    Side effects: none.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=TTSSynthesizeRequestSerializer,
        responses={
            200: TTSSynthesizeEnvelope,
            400: radio_error_envelope,
            401: radio_error_envelope,
        },
        summary="Synthesise text to audio",
        description=(
            "Synthesises the provided text into audio using the "
            "configured TTS engine (see `settings.TTS_ENGINE`). The "
            "audio bytes are returned base64-encoded under "
            "`data.audio_base64`."
        ),
        operation_id="v1_radio_tts_synthesize",
    )
    @timed("tts.synthesize")
    def post(self, request: Request) -> Response:
        """Validate input, synthesise, return envelope with base64 audio."""
        from alerts.tts import synthesize as tts_synthesize

        serializer = TTSSynthesizeRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        text = serializer.validated_data["text"]
        result = tts_synthesize(text)
        import base64

        encoded = base64.b64encode(result.audio_bytes).decode("ascii")
        return success_response(
            {
                "mime_type": result.mime_type,
                "duration_ms": result.duration_ms,
                "audio_base64": encoded,
            },
            message="TTS synthesis complete",
        )
