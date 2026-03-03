from __future__ import annotations

from typing import Any

import pytest
from django.core.cache import caches
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory

from config.views import CachedSpectacularAPIView
from weather.tasks import warm_openapi_schema_cache


@pytest.mark.django_db
def test_cached_schema_view_hits_cache(
    monkeypatch: pytest.MonkeyPatch,
    settings: Any,
) -> None:
    settings.SCHEMA_CACHE_TTL_SECONDS = 3600
    caches["default"].clear()
    calls = 0

    def fake_get(
        self: Any,
        request: Request,
        *args: object,
        **kwargs: object,
    ) -> Response:
        nonlocal calls
        calls += 1
        response = Response({"openapi": "3.0.3"})
        response["Content-Disposition"] = (
            'inline; filename="Weather APIs.yaml"'
        )
        return response

    monkeypatch.setattr(
        "drf_spectacular.views.SpectacularAPIView.get",
        fake_get,
    )

    factory = APIRequestFactory()
    view = CachedSpectacularAPIView.as_view()

    first = view(
        factory.get(
            "/api/schema/",
            HTTP_ACCEPT="application/vnd.oai.openapi",
        )
    )
    second = view(
        factory.get(
            "/api/schema/",
            HTTP_ACCEPT="application/vnd.oai.openapi",
        )
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert calls == 1
    assert second["Vary"] == "Accept"


@pytest.mark.django_db
def test_warm_schema_task_delegates_to_warmer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "weather.tasks.warm_openapi_schema_cache_variants",
        lambda: 2,
    )
    assert warm_openapi_schema_cache.run() == 2
