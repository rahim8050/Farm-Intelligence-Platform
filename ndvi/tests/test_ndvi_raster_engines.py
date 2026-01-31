from __future__ import annotations

# ruff: noqa: S101
import logging
import secrets
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast
from unittest.mock import MagicMock

import httpx
import numpy as np
import pytest
from django.test import override_settings
from rest_framework.exceptions import ValidationError

import ndvi.raster.stac_compute_engine as stac_compute_engine
from ndvi.engines.base import BBox
from ndvi.raster.base import RasterRequest
from ndvi.raster.sentinelhub_engine import (
    MAX_ERROR_SNIPPET_CHARS,
    SentinelHubRasterEngine,
    SentinelHubRasterError,
)
from ndvi.raster.stac_compute_engine import StacComputeRasterEngine
from ndvi.stac_client import NdviStats, StacClient, StacItem

CLIENT_SECRET = secrets.token_urlsafe(12)


@override_settings(NDVI_STAC_COLLECTION="collection")
def test_stac_compute_engine_encodes_png() -> None:
    engine = StacComputeRasterEngine()
    ndvi = np.array([[0.0, 0.5], [1.0, -1.0]], dtype=np.float32)
    png = engine._encode_png(ndvi)
    assert png.startswith(b"\x89PNG")


def _raster_request() -> RasterRequest:
    return RasterRequest(
        bbox=BBox(
            south=Decimal("0.0"),
            west=Decimal("0.0"),
            north=Decimal("0.1"),
            east=Decimal("0.1"),
        ),
        date=date(2025, 1, 1),
        size=128,
        max_cloud=20,
        engine="stac",
    )


def _stac_item(
    *, item_date: date, assets: dict[str, str], collection: str | None = None
) -> StacItem:
    return StacItem(
        id="item-1",
        datetime=datetime(
            item_date.year,
            item_date.month,
            item_date.day,
            tzinfo=UTC,
        ),
        assets=assets,
        cloud_cover=5.0,
        collection=collection,
    )


def test_stac_compute_engine_no_items_raises_not_found() -> None:
    class FakeClient:
        def search(
            self, *, bbox: object, start: object, end: object, max_cloud: int
        ) -> list[StacItem]:
            return []

    engine = StacComputeRasterEngine(
        client=cast(StacClient, FakeClient()),
        asset_red="B04",
        asset_nir="B08",
    )

    with pytest.raises(ValidationError) as exc:
        engine.render_png(_raster_request())

    detail = cast(dict[str, str], exc.value.detail)
    assert detail["detail"] == "Raster not found"
    assert detail["code"] == "raster_not_found"
    assert detail["reason"] == "no_items"


def test_stac_compute_engine_no_best_item_raises_not_found() -> None:
    class FakeClient:
        def search(
            self, *, bbox: object, start: object, end: object, max_cloud: int
        ) -> list[StacItem]:
            return [
                _stac_item(
                    item_date=date(2025, 2, 1),
                    assets={"B04": "red.tif", "B08": "nir.tif"},
                )
            ]

    engine = StacComputeRasterEngine(client=cast(StacClient, FakeClient()))

    with pytest.raises(ValidationError) as exc:
        engine.render_png(_raster_request())

    detail = cast(dict[str, str], exc.value.detail)
    assert detail["detail"] == "Raster not found"
    assert detail["code"] == "raster_not_found"
    assert detail["reason"] == "no_best_item"


def test_stac_compute_engine_missing_assets_raises_not_found() -> None:
    class FakeClient:
        def search(
            self, *, bbox: object, start: object, end: object, max_cloud: int
        ) -> list[StacItem]:
            return [
                _stac_item(
                    item_date=date(2025, 1, 1),
                    assets={"B04": "red.tif"},
                )
            ]

    engine = StacComputeRasterEngine(client=cast(StacClient, FakeClient()))

    with pytest.raises(ValidationError) as exc:
        engine.render_png(_raster_request())

    detail = cast(dict[str, str], exc.value.detail)
    assert detail["detail"] == "Raster not found"
    assert detail["code"] == "raster_not_found"
    assert detail["reason"] == "missing_assets"


def test_stac_compute_engine_missing_assets_logs_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FakeClient:
        collection = "sentinel-2-l2a"

        def search(
            self, *, bbox: object, start: object, end: object, max_cloud: int
        ) -> list[StacItem]:
            return [
                _stac_item(
                    item_date=date(2025, 1, 1),
                    assets={"B04": "red.tif"},
                    collection="sentinel-2-l2a",
                )
            ]

    engine = StacComputeRasterEngine(client=cast(StacClient, FakeClient()))

    with caplog.at_level(
        logging.WARNING, logger="ndvi.raster.stac_compute_engine"
    ):
        with pytest.raises(ValidationError):
            engine.render_png(_raster_request())

    message = " ".join(record.message for record in caplog.records)
    assert "collections=['sentinel-2-l2a']" in message
    assert "expected_assets={'red': ['B04_10m'" in message
    assert "'nir': ['B08_10m'" in message


def test_stac_compute_engine_missing_stats_raises_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        def search(
            self, *, bbox: object, start: object, end: object, max_cloud: int
        ) -> list[StacItem]:
            return [
                _stac_item(
                    item_date=date(2025, 1, 1),
                    assets={"B04": "red.tif", "B08": "nir.tif"},
                )
            ]

    monkeypatch.setattr(
        stac_compute_engine,
        "load_ndvi_array",
        lambda **_kwargs: np.array([[0.2]], dtype=np.float32),
    )
    monkeypatch.setattr(
        stac_compute_engine,
        "compute_ndvi_stats",
        lambda _ndvi: None,
    )
    engine = StacComputeRasterEngine(client=cast(StacClient, FakeClient()))

    with pytest.raises(ValidationError) as exc:
        engine.render_png(_raster_request())

    detail = cast(dict[str, str], exc.value.detail)
    assert detail["detail"] == "Raster not found"
    assert detail["code"] == "raster_not_found"
    assert detail["reason"] == "missing_assets"


def test_stac_compute_engine_renders_png_with_stats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        def search(
            self, *, bbox: object, start: object, end: object, max_cloud: int
        ) -> list[StacItem]:
            return [
                _stac_item(
                    item_date=date(2025, 1, 1),
                    assets={"B04": "red.tif", "B08": "nir.tif"},
                )
            ]

    monkeypatch.setattr(
        stac_compute_engine,
        "load_ndvi_array",
        lambda **_kwargs: np.array([[0.2]], dtype=np.float32),
    )
    monkeypatch.setattr(
        stac_compute_engine,
        "compute_ndvi_stats",
        lambda _ndvi: NdviStats(
            mean=0.2,
            min=0.1,
            max=0.3,
            sample_count=1,
        ),
    )
    engine = StacComputeRasterEngine(client=cast(StacClient, FakeClient()))
    png = engine.render_png(_raster_request())
    assert png.startswith(b"\x89PNG")


def test_stac_compute_engine_falls_back_to_suffixed_assets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        def search(
            self, *, bbox: object, start: object, end: object, max_cloud: int
        ) -> list[StacItem]:
            return [
                _stac_item(
                    item_date=date(2025, 1, 1),
                    assets={
                        "B04_10m": "red-10m.tif",
                        "B08_10m": "nir-10m.tif",
                    },
                )
            ]

    captured: dict[str, str] = {}

    def fake_load_ndvi_array(**kwargs: object) -> np.ndarray:
        captured["red_href"] = cast(str, kwargs["red_href"])
        captured["nir_href"] = cast(str, kwargs["nir_href"])
        return np.array([[0.2]], dtype=np.float32)

    monkeypatch.setattr(
        stac_compute_engine, "load_ndvi_array", fake_load_ndvi_array
    )
    monkeypatch.setattr(
        stac_compute_engine,
        "compute_ndvi_stats",
        lambda _ndvi: NdviStats(
            mean=0.2,
            min=0.1,
            max=0.3,
            sample_count=1,
        ),
    )
    engine = StacComputeRasterEngine(
        client=cast(StacClient, FakeClient()),
        asset_red="B04",
        asset_nir="B08",
    )
    engine.render_png(_raster_request())
    assert captured["red_href"] == "red-10m.tif"
    assert captured["nir_href"] == "nir-10m.tif"


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
