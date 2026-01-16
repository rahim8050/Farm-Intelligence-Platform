from __future__ import annotations

# ruff: noqa: S101
import secrets
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

import httpx
import pytest

from ndvi.engines.base import BBox
from ndvi.raster.base import RasterRequest
from ndvi.raster.sentinelhub_engine import (
    MAX_ERROR_SNIPPET_CHARS,
    SentinelHubRasterEngine,
    SentinelHubRasterError,
)
from ndvi.raster.stac_compute_engine import StacComputeRasterEngine

CLIENT_SECRET = secrets.token_urlsafe(12)


def test_stac_compute_engine_not_implemented() -> None:
    engine = StacComputeRasterEngine()
    request = RasterRequest(
        bbox=BBox(
            south=Decimal("0.0"),
            west=Decimal("0.0"),
            north=Decimal("0.1"),
            east=Decimal("0.1"),
        ),
        date=date(2025, 1, 1),
        size=256,
        max_cloud=30,
        engine="stac",
    )
    with pytest.raises(NotImplementedError):
        engine.render_png(request)


def test_sentinelhub_raster_render_png_uses_token() -> None:
    engine = SentinelHubRasterEngine(
        client_id="cid",
        client_secret=CLIENT_SECRET,
        base_url="https://example.com",
    )
    engine._stats._get_access_token = MagicMock(return_value="token")  # type: ignore[assignment]

    class FakeResponse:
        content = b"png-bytes"

    engine._request_with_retry = MagicMock(return_value=FakeResponse())  # type: ignore[assignment]
    request = RasterRequest(
        bbox=BBox(
            south=Decimal("0.0"),
            west=Decimal("0.0"),
            north=Decimal("0.1"),
            east=Decimal("0.1"),
        ),
        date=date(2025, 1, 1),
        size=128,
        max_cloud=20,
        engine="sentinelhub",
    )
    result = engine.render_png(request)
    assert result == b"png-bytes"


def test_sentinelhub_raster_build_payload() -> None:
    engine = SentinelHubRasterEngine(
        client_id="cid",
        client_secret=CLIENT_SECRET,
        base_url="https://example.com",
    )
    request = RasterRequest(
        bbox=BBox(
            south=Decimal("1.0"),
            west=Decimal("2.0"),
            north=Decimal("3.0"),
            east=Decimal("4.0"),
        ),
        date=date(2025, 1, 2),
        size=256,
        max_cloud=10,
        engine="sentinelhub",
    )
    payload = engine._build_payload(request)
    data_filter = payload["input"]["data"][0]["dataFilter"]
    assert payload["input"]["bounds"]["bbox"] == [2.0, 1.0, 4.0, 3.0]
    assert payload["output"]["width"] == 256
    assert payload["output"]["height"] == 256
    assert "aggregation" not in payload
    assert payload["evalscript"]
    assert data_filter["maxCloudCoverage"] == 10
    assert "timeRange" in data_filter
    assert data_filter["timeRange"]["from"].endswith("Z")
    assert data_filter["timeRange"]["to"].endswith("Z")


def test_sentinelhub_raster_render_png_sends_process_payload() -> None:
    engine = SentinelHubRasterEngine(
        client_id="cid",
        client_secret=CLIENT_SECRET,
        base_url="https://example.com",
    )
    engine._stats._get_access_token = MagicMock(return_value="token")  # type: ignore[assignment]

    class FakeResponse:
        content = b"png-bytes"

    engine._request_with_retry = MagicMock(return_value=FakeResponse())  # type: ignore[assignment]
    request = RasterRequest(
        bbox=BBox(
            south=Decimal("0.0"),
            west=Decimal("0.0"),
            north=Decimal("0.1"),
            east=Decimal("0.1"),
        ),
        date=date(2025, 1, 1),
        size=128,
        max_cloud=20,
        engine="sentinelhub",
    )
    result = engine.render_png(request)
    assert result == b"png-bytes"

    engine._request_with_retry.assert_called_once()
    method, url = engine._request_with_retry.call_args.args[:2]
    payload = engine._request_with_retry.call_args.kwargs["json"]
    assert method == "POST"
    assert url == engine.process_url
    assert "aggregation" not in payload
    assert payload["evalscript"]
    data_filter = payload["input"]["data"][0]["dataFilter"]
    assert "timeRange" in data_filter


def test_sentinelhub_raster_request_retries_on_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = SentinelHubRasterEngine(
        client_id="cid",
        client_secret=CLIENT_SECRET,
        base_url="https://example.com",
    )
    calls = {"count": 0}

    class FakeResponse:
        def __init__(self, status_code: int = 200) -> None:
            self.status_code = status_code

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                request = httpx.Request("GET", "https://example.com")
                response = httpx.Response(self.status_code)
                raise httpx.HTTPStatusError(
                    "boom", request=request, response=response
                )

    def fake_request(*_: object, **__: object) -> FakeResponse:
        calls["count"] += 1
        if calls["count"] == 1:
            return FakeResponse(status_code=502)
        return FakeResponse(status_code=200)

    monkeypatch.setattr(engine._http, "request", fake_request)
    monkeypatch.setattr(
        "ndvi.raster.sentinelhub_engine.time.sleep", lambda *_: None
    )
    resp = engine._request_with_retry(
        "POST", "https://example.com", json={"ok": True}
    )
    assert isinstance(resp, FakeResponse)
    assert calls["count"] == 2


def test_sentinelhub_raster_request_raises_request_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = SentinelHubRasterEngine(
        client_id="cid",
        client_secret=CLIENT_SECRET,
        base_url="https://example.com",
    )

    def fake_request(*_: object, **__: object) -> None:
        raise httpx.RequestError("network", request=httpx.Request("GET", "x"))

    monkeypatch.setattr(engine._http, "request", fake_request)
    monkeypatch.setattr(
        "ndvi.raster.sentinelhub_engine.time.sleep", lambda *_: None
    )
    with pytest.raises(httpx.RequestError):
        engine._request_with_retry(
            "POST", "https://example.com", json={"ok": True}, max_attempts=2
        )


def test_sentinelhub_raster_request_zero_attempts() -> None:
    engine = SentinelHubRasterEngine(
        client_id="cid",
        client_secret=CLIENT_SECRET,
        base_url="https://example.com",
    )
    with pytest.raises(RuntimeError, match="Unknown raster upstream error"):
        engine._request_with_retry(
            "POST", "https://example.com", json={"ok": True}, max_attempts=0
        )


def test_sentinelhub_raster_request_http_error_includes_snippet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = SentinelHubRasterEngine(
        client_id="cid",
        client_secret=CLIENT_SECRET,
        base_url="https://example.com",
    )
    long_body = "error " * 1000

    class FakeResponse:
        status_code = 400

        def __init__(self) -> None:
            self.request = httpx.Request("POST", "https://example.com")
            self._text = long_body

        def raise_for_status(self) -> None:
            response = httpx.Response(
                status_code=400,
                request=self.request,
                content=self._text.encode(),
                headers={"Content-Type": "text/plain"},
            )
            raise httpx.HTTPStatusError(
                "boom", request=self.request, response=response
            )

    def fake_request(*_: object, **__: object) -> FakeResponse:
        return FakeResponse()

    monkeypatch.setattr(engine._http, "request", fake_request)
    with pytest.raises(SentinelHubRasterError) as exc_info:
        engine._request_with_retry(
            "POST",
            "https://example.com",
            json={"ok": True},
            max_attempts=1,
        )
    error = exc_info.value
    assert error.status_code == 400
    assert "status=400" in str(error)
    assert error.snippet is not None
    assert len(error.snippet) <= MAX_ERROR_SNIPPET_CHARS + 3
    assert error.snippet.endswith("...")
