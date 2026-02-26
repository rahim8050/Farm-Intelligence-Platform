"""NDVI proxy endpoints.

Authentication: JWT or API key (global defaults).
Responses: standard envelope forwarded from the NDVI microservice when enabled.
"""

from __future__ import annotations

from django.conf import settings
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from config.api.openapi import (
    error_envelope_serializer,
    success_envelope_serializer,
)
from config.api.proxy import proxy_json_request
from config.api.responses import error_response

from .serializers import NdviIngestSerializer

ndvi_ingest_success_schema = success_envelope_serializer(
    "NdviIngestSuccess",
    data=inline_serializer(
        name="NdviIngestData",
        fields={"status": serializers.CharField()},
    ),
)
ndvi_info_success_schema = success_envelope_serializer(
    "NdviIngestInfo",
    data=inline_serializer(
        name="NdviIngestInfoData",
        fields={"message": serializers.CharField()},
    ),
)
ndvi_error_schema = error_envelope_serializer("NdviProxyError")


class NdviIngestProxyView(APIView):
    """Proxy NDVI ingestion to the Rust microservice.

    Auth: IsAuthenticated (JWT or API key).
    Response: envelope forwarded from the NDVI service.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=NdviIngestSerializer,
        responses={
            201: ndvi_ingest_success_schema,
            400: ndvi_error_schema,
            401: ndvi_error_schema,
            403: ndvi_error_schema,
            503: ndvi_error_schema,
        },
    )
    def post(self, request: Request) -> Response:
        """Validate input and forward to the NDVI service."""

        serializer = NdviIngestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if not settings.NDVI_PROXY_ENABLED:
            return error_response(
                "NDVI service proxy disabled",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return proxy_json_request(
            request,
            settings.NDVI_SERVICE_URL,
            "/api/v1/ndvi",
            json_body=serializer.data,
        )

    @extend_schema(
        responses={
            200: ndvi_info_success_schema,
            401: ndvi_error_schema,
            403: ndvi_error_schema,
            503: ndvi_error_schema,
        },
    )
    def get(self, request: Request) -> Response:
        """Return the NDVI ingest info from the upstream service."""

        if not settings.NDVI_PROXY_ENABLED:
            return error_response(
                "NDVI service proxy disabled",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return proxy_json_request(
            request,
            settings.NDVI_SERVICE_URL,
            "/api/v1/ndvi",
        )
