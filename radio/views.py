"""Radio API endpoints.

This module provides endpoints for radio station discovery and streaming.

Auth: Public access (no authentication required)
Response: All responses use success_response envelope.
"""

from __future__ import annotations

from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from config.api.responses import success_response
from radio.models import Provider, Station
from radio.serializers import (
    ProviderSerializer,
    StationDetailSerializer,
    StationSerializer,
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

    Auth: Public
    Throttle: None
    Response: envelope with `data` containing stream_url and format.
    """

    permission_classes = [AllowAny]

    @extend_schema(
        responses={200: StreamUrlEnvelope},
        summary="Get stream URL",
        description="Returns stream URL and metadata for playback.",
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
            {
                "stream_url": station.stream_url,
                "format": station.format,
                "bitrate": station.bitrate,
                "station_name": station.name,
            },
            message="Stream URL retrieved successfully",
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
