from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, cast, overload

import httpx
from django.conf import settings
from django.core.cache import caches
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response

from config.api.responses import JSONValue, error_response

_FORWARD_HEADERS: tuple[str, ...] = (
    "authorization",
    "x-api-key",
    "x-request-id",
    "x-correlation-id",
    "x-client-id",
    "x-timestamp",
    "x-nonce",
    "x-signature",
)


def _forward_headers(request: Request) -> dict[str, str]:
    headers: dict[str, str] = {}
    for header in _FORWARD_HEADERS:
        value = request.headers.get(header)
        if value:
            headers[header] = value
    return headers


@overload
def proxy_json_request(
    request: Request,
    upstream_base_url: str,
    upstream_path: str,
    *,
    json_body: JSONValue | None = None,
    params: Mapping[str, str] | None = None,
    cache_key: str | None = None,
    cache_ttl_s: int | None = None,
    fallback_on_error: Literal[False] = False,
) -> Response: ...


@overload
def proxy_json_request(
    request: Request,
    upstream_base_url: str,
    upstream_path: str,
    *,
    json_body: JSONValue | None = None,
    params: Mapping[str, str] | None = None,
    cache_key: str | None = None,
    cache_ttl_s: int | None = None,
    fallback_on_error: Literal[True],
) -> Response | None: ...


def proxy_json_request(
    request: Request,
    upstream_base_url: str,
    upstream_path: str,
    *,
    json_body: JSONValue | None = None,
    params: Mapping[str, str] | None = None,
    cache_key: str | None = None,
    cache_ttl_s: int | None = None,
    fallback_on_error: bool = False,
) -> Response | None:
    """Forward the incoming request to an upstream JSON service."""

    if not upstream_base_url:
        if fallback_on_error:
            return None
        return error_response(
            "Upstream service not configured",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    url = f"{upstream_base_url.rstrip('/')}{upstream_path}"
    headers = _forward_headers(request)
    timeout = float(getattr(settings, "PROXY_TIMEOUT_SECONDS", 10.0))
    query = request.query_params.dict() if params is None else params
    body = json_body
    can_cache = (
        request.method == "GET"
        and bool(cache_key)
        and cache_ttl_s is not None
        and cache_ttl_s > 0
    )
    resolved_cache_key = cache_key or ""
    resolved_cache_ttl = int(cache_ttl_s or 0)

    if can_cache:
        cached_payload = caches["default"].get(resolved_cache_key)
        if cached_payload is not None:
            return Response(cached_payload, status=status.HTTP_200_OK)

    if body is None and request.method in {"POST", "PUT", "PATCH"}:
        body = request.data if request.data else None

    try:
        response = httpx.request(
            cast(str, request.method),
            url,
            params=query,
            json=body,
            headers=headers,
            timeout=timeout,
        )
    except httpx.RequestError:
        if fallback_on_error:
            return None
        return error_response(
            "Upstream service unavailable",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            payload = response.json()
        except ValueError:
            return error_response(
                "Upstream returned invalid JSON",
                status_code=status.HTTP_502_BAD_GATEWAY,
            )
        if can_cache and response.status_code == status.HTTP_200_OK:
            caches["default"].set(
                resolved_cache_key,
                payload,
                timeout=resolved_cache_ttl,
            )
        return Response(payload, status=response.status_code)

    return Response(
        response.text,
        status=response.status_code,
        content_type=content_type or "text/plain",
    )
