from __future__ import annotations

# ruff: noqa: S101
from decimal import Decimal
from json import JSONDecodeError
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from django.core.cache import caches
from django.http import HttpResponse
from django.test import Client, RequestFactory
from rest_framework.exceptions import Throttled
from rest_framework.parsers import JSONParser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory

from config.api.exceptions import _to_json_value, custom_exception_handler
from config.api.openapi import remove_deprecated_integration_aliases
from config.api.proxy import proxy_json_request
from config.api.responses import error_response
from config.views import prometheus_metrics


def test_error_response_payload() -> None:
    resp = error_response(
        "Bad request",
        errors={"field": ["missing"]},
        status_code=418,
    )
    assert resp.status_code == 418
    assert resp.data["status"] == 1
    assert resp.data["message"] == "Bad request"
    assert resp.data["data"] is None
    assert resp.data["errors"] == {"field": ["missing"]}


def test_custom_exception_handler_returns_500_on_unhandled() -> None:
    with patch("rest_framework.views.exception_handler", return_value=None):
        resp = custom_exception_handler(Exception("boom"), {})
    assert resp.status_code == 500
    assert resp.data["status"] == 1
    assert resp.data["message"] == "Internal server error"


def test_custom_exception_handler_throttled_non_dict_detail() -> None:
    exc = Throttled(wait=12)
    with patch(
        "rest_framework.views.exception_handler",
        return_value=Response("slow down", status=429),
    ):
        resp = custom_exception_handler(exc, {})
    assert resp.status_code == 429
    assert resp.data["message"] == "Too Many Requests"
    assert resp.data["errors"]["detail"] == "slow down"
    assert resp.data["errors"]["wait"] == 12


def test_to_json_value_handles_sequences() -> None:
    payload = ("ok", {"value": Decimal("1.25")})
    assert _to_json_value(payload) == ["ok", {"value": "1.25"}]


def test_home_view_returns_metadata() -> None:
    client = Client()
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["service"] == "farm-intelligence-platform"
    assert body["docs"] == "/api/docs/"


def test_prometheus_metrics_recovers_from_corrupt_multiprocess_files(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics_dir = tmp_path / "prometheus"
    metrics_dir.mkdir()
    stale_file = metrics_dir / "counter_123.db"
    stale_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(metrics_dir))
    request = RequestFactory().get("/metrics")

    with patch(
        "config.views.ExportToDjangoView",
        side_effect=[
            JSONDecodeError("Expecting value", "", 0),
            HttpResponse(b"metrics-ok", content_type="text/plain"),
        ],
    ) as mock_export:
        response = prometheus_metrics(request)

    assert response.status_code == 200
    assert response.content == b"metrics-ok"
    assert not stale_file.exists()
    assert mock_export.call_count == 2


def test_remove_deprecated_aliases_ignores_non_dict_paths() -> None:
    result: dict[str, Any] = {"openapi": "3.0.0", "paths": []}
    assert (
        remove_deprecated_integration_aliases(result, None, None, True)
        == result
    )


def test_remove_deprecated_aliases_strips_deprecated_paths() -> None:
    result: dict[str, Any] = {
        "paths": {
            "/api/v1/integration/ping/": {"get": {}},
            "/api/v1/integrations/integrations/ping/": {"get": {}},
            "/api/v1/integrations/nextcloud/ping/": {"get": {}},
        }
    }
    updated = remove_deprecated_integration_aliases(result, None, None, True)
    assert "/api/v1/integrations/nextcloud/ping/" in updated["paths"]
    assert "/api/v1/integration/ping/" not in updated["paths"]
    assert "/api/v1/integrations/integrations/ping/" not in updated["paths"]


def test_proxy_json_request_returns_503_when_upstream_missing() -> None:
    request = Request(APIRequestFactory().get("/proxy"))

    response = proxy_json_request(request, "", "/api/v1/upstream")

    assert response is not None
    assert response.status_code == 503
    assert response.data["message"] == "Upstream service not configured"


def test_proxy_json_request_missing_upstream_with_fallback() -> None:
    request = Request(APIRequestFactory().get("/proxy"))

    response = proxy_json_request(
        request,
        "",
        "/api/v1/upstream",
        fallback_on_error=True,
    )

    assert response is None


@patch("config.api.proxy.httpx.request")
def test_proxy_json_request_returns_cached_payload(
    mock_request: Any,
) -> None:
    caches["default"].clear()
    cached_payload = {"status": 0, "message": "OK", "data": {"cached": True}}
    caches["default"].set("proxy-cache-key", cached_payload, timeout=60)
    request = Request(APIRequestFactory().get("/proxy"))

    response = proxy_json_request(
        request,
        "http://upstream",
        "/api/v1/upstream",
        cache_key="proxy-cache-key",
        cache_ttl_s=60,
    )

    assert response is not None
    assert response.status_code == 200
    assert response.data == cached_payload
    mock_request.assert_not_called()


@patch("config.api.proxy.httpx.request")
@patch("config.api.proxy.settings.PROXY_TIMEOUT_SECONDS", 10.0)
def test_proxy_json_request_uses_request_data_for_post(
    mock_request: Any,
) -> None:
    request = Request(
        APIRequestFactory().post(
            "/proxy",
            {"value": 1},
            format="json",
            HTTP_AUTHORIZATION="Bearer token",
        ),
        parsers=[JSONParser()],
    )
    mock_request.return_value = httpx.Response(
        status_code=201,
        json={"status": 0, "message": "OK", "data": {"created": True}},
        headers={"content-type": "application/json"},
        request=httpx.Request("POST", "http://upstream/api/v1/upstream"),
    )

    response = proxy_json_request(
        request, "http://upstream", "/api/v1/upstream"
    )

    assert response is not None
    assert response.status_code == 201
    mock_request.assert_called_once_with(
        "POST",
        "http://upstream/api/v1/upstream",
        params={},
        json={"value": 1},
        headers={"authorization": "Bearer token"},
        timeout=10.0,
    )


@patch("config.api.proxy.httpx.request")
def test_proxy_json_request_handles_request_error_without_fallback(
    mock_request: Any,
) -> None:
    request = Request(APIRequestFactory().get("/proxy"))
    mock_request.side_effect = httpx.RequestError(
        "boom",
        request=httpx.Request("GET", "http://upstream/api/v1/upstream"),
    )

    response = proxy_json_request(
        request, "http://upstream", "/api/v1/upstream"
    )

    assert response is not None
    assert response.status_code == 503
    assert response.data["message"] == "Upstream service unavailable"


@patch("config.api.proxy.httpx.request")
def test_proxy_json_request_handles_request_error_with_fallback(
    mock_request: Any,
) -> None:
    request = Request(APIRequestFactory().get("/proxy"))
    mock_request.side_effect = httpx.RequestError(
        "boom",
        request=httpx.Request("GET", "http://upstream/api/v1/upstream"),
    )

    response = proxy_json_request(
        request,
        "http://upstream",
        "/api/v1/upstream",
        fallback_on_error=True,
    )

    assert response is None


@patch("config.api.proxy.httpx.request")
def test_proxy_json_request_handles_invalid_json(
    mock_request: Any,
) -> None:
    request = Request(APIRequestFactory().get("/proxy"))
    upstream_response = httpx.Response(
        status_code=200,
        text="not-json",
        headers={"content-type": "application/json"},
        request=httpx.Request("GET", "http://upstream/api/v1/upstream"),
    )

    def raise_value_error() -> Any:
        raise ValueError("invalid")

    upstream_response.json = raise_value_error  # type: ignore[assignment]
    mock_request.return_value = upstream_response

    response = proxy_json_request(
        request, "http://upstream", "/api/v1/upstream"
    )

    assert response is not None
    assert response.status_code == 502
    assert response.data["message"] == "Upstream returned invalid JSON"


@patch("config.api.proxy.httpx.request")
def test_proxy_json_request_returns_non_json_response_text(
    mock_request: Any,
) -> None:
    request = Request(APIRequestFactory().get("/proxy"))
    mock_request.return_value = httpx.Response(
        status_code=202,
        text="plain-text",
        headers={"content-type": "text/plain"},
        request=httpx.Request("GET", "http://upstream/api/v1/upstream"),
    )

    response = proxy_json_request(
        request, "http://upstream", "/api/v1/upstream"
    )

    assert response is not None
    assert response.status_code == 202
    assert response.content_type == "text/plain"
    assert response.data == "plain-text"
    assert response.data == "plain-text"
