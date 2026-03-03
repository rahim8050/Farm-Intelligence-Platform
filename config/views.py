"""Project-level non-DRF views.

This module contains the root landing endpoint used for quick service checks
and links to the interactive API documentation endpoints.
"""

from __future__ import annotations

import hashlib
from typing import Any, TypedDict, cast

from django.conf import settings
from django.core.cache import caches
from django.http import HttpRequest, JsonResponse
from django.test.client import RequestFactory
from drf_spectacular.views import SpectacularAPIView
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response


class SchemaCacheEntry(TypedDict):
    """Cached OpenAPI response payload metadata."""

    data: Any
    status_code: int
    content_disposition: str


def _get_schema_cache_ttl_seconds() -> int:
    return int(getattr(settings, "SCHEMA_CACHE_TTL_SECONDS", 3600))


def _schema_cache_key(request: Request) -> str:
    query_string = str(request.META.get("QUERY_STRING", ""))
    accept = str(request.META.get("HTTP_ACCEPT", ""))
    key_source = f"{request.path}?{query_string}|{accept}"
    digest = hashlib.sha256(key_source.encode("utf-8")).hexdigest()
    return f"schema:openapi:{digest}"


def home(request: HttpRequest) -> JsonResponse:
    """Return basic service metadata and documentation links."""
    return JsonResponse(
        {
            "ok": True,
            "service": "weather-apis",
            "docs": "/api/docs/",
            "redoc": "/api/redoc/",
        }
    )


class CachedSpectacularAPIView(SpectacularAPIView):
    """Serve the OpenAPI schema with Redis-backed caching."""

    schema = None

    def get(
        self, request: Request, *args: object, **kwargs: object
    ) -> Response:
        cache = caches["default"]
        cache_key = _schema_cache_key(request)
        cached = cache.get(cache_key)
        if isinstance(cached, dict) and "data" in cached:
            entry = cast(SchemaCacheEntry, cached)
            response = Response(
                entry["data"],
                status=int(entry["status_code"]),
            )
            content_disposition = str(entry["content_disposition"])
            if content_disposition:
                response["Content-Disposition"] = content_disposition
            response["Vary"] = "Accept"
            return response

        response = cast(Response, super().get(request, *args, **kwargs))
        if response.status_code == status.HTTP_200_OK:
            cache.set(
                cache_key,
                SchemaCacheEntry(
                    data=response.data,
                    status_code=response.status_code,
                    content_disposition=str(
                        response.get("Content-Disposition", "")
                    ),
                ),
                timeout=_get_schema_cache_ttl_seconds(),
            )
        return response


def warm_openapi_schema_cache_variants() -> int:
    """Warm OpenAPI schema cache entries for common response variants."""

    factory = RequestFactory()
    view = CachedSpectacularAPIView.as_view()
    variants: tuple[tuple[dict[str, str], str], ...] = (
        ({}, "application/vnd.oai.openapi"),
        ({"format": "json"}, "application/json"),
    )
    warmed = 0
    for query_params, accept in variants:
        request = factory.get(
            "/api/schema/",
            data=query_params,
            HTTP_ACCEPT=accept,
        )
        response = view(request)
        if response.status_code == status.HTTP_200_OK:
            warmed += 1
    return warmed
