from __future__ import annotations

import importlib
import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import cast

import httpx
import numpy as np
import pytest
from django.test import override_settings

import ndvi.stac_client as stac_module
from ndvi.engines.base import BBox
from ndvi.stac_client import (
    MAX_ERROR_SNIPPET_CHARS,
    StacClient,
    StacDependencyError,
    StacItem,
    StacProcessingError,
    StacUpstreamError,
    _parse_datetime,
    compute_ndvi_stats,
    filter_items_by_cloud,
    load_ndvi_array,
    normalize_cloud_fraction,
    normalize_stac_bbox,
    resolve_asset_href,
    select_best_item,
)


def test_stac_client_search_filters_and_selects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = StacClient(
        base_url="https://example.com/stac/",
        collection="collection",
        timeout_seconds=1,
    )
    response_data: dict[str, object] = {
        "features": [
            {
                "id": "low-cloud-close",
                "properties": {
                    "datetime": "2025-01-02T10:00:00Z",
                    "eo:cloud_cover": 10,
                },
                "assets": {"B04": {"href": "https://example.com/red.tif"}},
            },
            {
                "id": "high-cloud",
                "properties": {
                    "datetime": "2025-01-02T10:00:00Z",
                    "eo:cloud_cover": 80,
                },
                "assets": {"B04": {"href": "https://example.com/red2.tif"}},
            },
            {
                "id": "no-cloud",
                "properties": {"datetime": "2025-01-03T10:00:00Z"},
                "assets": {"B04": {"href": "https://example.com/red3.tif"}},
            },
        ],
        "links": [],
    }

    class FakeResponse:
        content = b'{"features": [], "links": []}'

        def json(self) -> dict[str, object]:
            return response_data

    monkeypatch.setattr(
        client, "_request", lambda *_args, **_kwargs: FakeResponse()
    )

    items = client.search(
        bbox=BBox(
            south=Decimal("0.0"),
            west=Decimal("0.0"),
            north=Decimal("0.1"),
            east=Decimal("0.1"),
        ),
        start=date(2025, 1, 1),
        end=date(2025, 1, 5),
        max_cloud=30,
    )
    assert len(items) == 2

    best = select_best_item(
        items,
        target_date=date(2025, 1, 2),
        window_days=3,
    )
    assert best is not None
    assert best.id == "low-cloud-close"


def test_normalize_stac_bbox_swaps_lat_lon_order() -> None:
    normalized = normalize_stac_bbox([-0.92234, 36.78345, -0.92202, 36.78411])
    assert normalized == (
        pytest.approx(36.78345),
        pytest.approx(-0.92234),
        pytest.approx(36.78411),
        pytest.approx(-0.92202),
    )


def test_normalize_stac_bbox_keeps_lon_lat_order() -> None:
    normalized = normalize_stac_bbox([36.78345, -0.92234, 36.78411, -0.92202])
    assert normalized == (
        pytest.approx(36.78345),
        pytest.approx(-0.92234),
        pytest.approx(36.78411),
        pytest.approx(-0.92202),
    )


def test_normalize_stac_bbox_invalid_raises() -> None:
    with pytest.raises(ValueError):
        normalize_stac_bbox([200.0, 10.0, 201.0, 11.0])


def test_normalize_stac_bbox_logs_swap_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="ndvi.stac_client"):
        normalize_stac_bbox(
            [-0.92234, 36.78345, -0.92202, 36.78411],
            farm_id=33,
            job_id=44,
        )
    message = " ".join(record.message for record in caplog.records)
    assert "ndvi.stac.bbox_swapped" in message
    assert "farm_id=33" in message
    assert "job_id=44" in message


def test_stac_client_search_payload_uses_lon_lat_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = StacClient(
        base_url="https://example.com/stac/",
        collection="collection",
        timeout_seconds=1,
    )
    captured: dict[str, object] = {}

    class FakeResponse:
        content = b'{"features": [], "links": []}'

        def json(self) -> dict[str, object]:
            return {"features": [], "links": []}

    def fake_request(
        _method: str, _url: str, *, json: dict[str, object] | None = None
    ) -> FakeResponse:
        captured["bbox"] = json["bbox"] if json else None
        return FakeResponse()

    monkeypatch.setattr(client, "_request", fake_request)
    items = client.search(
        bbox=[-0.92234, 36.78345, -0.92202, 36.78411],
        start=date(2025, 1, 1),
        end=date(2025, 1, 3),
        max_cloud=30,
    )
    assert items == []
    assert captured["bbox"] == [
        pytest.approx(36.78345),
        pytest.approx(-0.92234),
        pytest.approx(36.78411),
        pytest.approx(-0.92202),
    ]


def test_parse_datetime_accepts_z_suffix_and_rejects_invalid() -> None:
    assert _parse_datetime(None) is None
    parsed = _parse_datetime("2025-01-02T10:00:00Z")
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert _parse_datetime("  ") is None
    assert _parse_datetime("bogus") is None


def test_resolve_asset_href_is_case_insensitive_and_missing() -> None:
    item = StacItem(
        id="item-1",
        datetime=datetime(2025, 1, 1, tzinfo=UTC),
        assets={"b04": "red.tif"},
        cloud_cover=None,
    )
    assert resolve_asset_href(item, "B04") == "red.tif"
    assert resolve_asset_href(item, "B08") is None


def test_stac_client_init_requires_base_url() -> None:
    with pytest.raises(ValueError):
        StacClient(
            base_url=" ",
            collection="collection",
            timeout_seconds=1,
        )


@override_settings(NDVI_STAC_COLLECTION="")
def test_stac_client_init_requires_collection() -> None:
    with pytest.raises(ValueError):
        StacClient(
            base_url="https://example.com/stac/",
            timeout_seconds=1,
        )


def test_normalize_cloud_fraction_handles_none_and_percent() -> None:
    assert normalize_cloud_fraction(None) is None
    assert normalize_cloud_fraction(50.0) == 0.5
    assert normalize_cloud_fraction(0.4) == 0.4


def test_filter_items_by_cloud_allows_missing_cloud() -> None:
    items = [
        StacItem(
            id="ok",
            datetime=datetime(2025, 1, 1, tzinfo=UTC),
            assets={},
            cloud_cover=None,
        ),
        StacItem(
            id="nope",
            datetime=datetime(2025, 1, 1, tzinfo=UTC),
            assets={},
            cloud_cover=80.0,
        ),
    ]
    filtered = filter_items_by_cloud(items, max_cloud=30)
    assert [item.id for item in filtered] == ["ok"]


def test_compute_ndvi_stats_handles_empty_and_nan() -> None:
    assert compute_ndvi_stats(np.array([], dtype=np.float32)) is None
    assert compute_ndvi_stats(np.array([[np.nan]], dtype=np.float32)) is None
    stats = compute_ndvi_stats(np.array([[0.1, 0.3]], dtype=np.float32))
    assert stats is not None
    assert stats.sample_count == 2


def test_parse_items_filters_invalid_and_parses_cloud() -> None:
    client = StacClient(
        base_url="https://example.com/stac/",
        collection="collection",
        timeout_seconds=1,
    )
    data: dict[str, object] = {
        "features": [
            "not-a-dict",
            {"id": "missing-dt", "properties": {}},
            {"id": "invalid-dt", "properties": {"datetime": "bogus"}},
            {
                "id": "valid",
                "properties": {
                    "datetime": "2025-01-02T10:00:00Z",
                    "cloud_cover": "12",
                },
                "assets": {"B04": {"href": "red.tif"}},
                "links": [
                    {
                        "rel": "self",
                        "href": "https://example.com/stac/items/1",
                    }
                ],
            },
        ],
        "links": [],
    }
    items = client._parse_items(data)
    assert len(items) == 1
    assert items[0].cloud_cover == 12.0
    assert items[0].assets["B04"] == "https://example.com/stac/items/red.tif"


def test_parse_items_returns_empty_when_features_not_list() -> None:
    client = StacClient(
        base_url="https://example.com/stac/",
        collection="collection",
        timeout_seconds=1,
    )
    assert client._parse_items({"features": "nope"}) == []


def test_parse_items_invalid_cloud_cover_sets_none() -> None:
    client = StacClient(
        base_url="https://example.com/stac/",
        collection="collection",
        timeout_seconds=1,
    )
    data: dict[str, object] = {
        "features": [
            {
                "id": "invalid-cloud",
                "properties": {
                    "datetime": "2025-01-02T10:00:00Z",
                    "eo:cloud_cover": "bad",
                },
                "assets": {"B04": {"href": "red.tif"}},
            }
        ],
        "links": [],
    }
    items = client._parse_items(data)
    assert len(items) == 1
    assert items[0].cloud_cover is None


def test_parse_assets_skips_invalid_and_uses_base_href() -> None:
    client = StacClient(
        base_url="https://example.com/stac/",
        collection="collection",
        timeout_seconds=1,
    )
    feature = {
        "assets": {
            "B04": {"href": "red.tif"},
            "B08": {"href": ""},
            "bad": "nope",
        },
        "links": [{"rel": "self", "href": "https://example.com/stac/items/1"}],
    }
    assets = client._parse_assets(feature)
    assert assets == {"B04": "https://example.com/stac/items/red.tif"}


def test_parse_assets_returns_empty_when_assets_not_dict() -> None:
    client = StacClient(
        base_url="https://example.com/stac/",
        collection="collection",
        timeout_seconds=1,
    )
    assert client._parse_assets({"assets": "nope"}) == {}


def test_parse_assets_ignores_bad_links_and_missing_href() -> None:
    client = StacClient(
        base_url="https://example.com/stac/",
        collection="collection",
        timeout_seconds=1,
    )
    feature = {
        "assets": {"B04": {"href": "red.tif"}},
        "links": ["bad", {"rel": "self", "href": ""}],
    }
    assets = client._parse_assets(feature)
    assert assets == {"B04": "https://example.com/stac/red.tif"}


def test_response_snippet_truncates_and_handles_none() -> None:
    client = StacClient(
        base_url="https://example.com/stac/",
        collection="collection",
        timeout_seconds=1,
    )
    assert client._response_snippet(None) is None
    empty = httpx.Response(
        200,
        request=httpx.Request("GET", "https://example.com"),
        text="",
    )
    assert client._response_snippet(empty) is None
    body = "a" * (MAX_ERROR_SNIPPET_CHARS + 10)
    response = httpx.Response(
        400,
        request=httpx.Request("GET", "https://example.com"),
        text=body,
    )
    snippet = client._response_snippet(response)
    assert snippet is not None
    assert snippet.endswith("...")
    assert len(snippet) == MAX_ERROR_SNIPPET_CHARS + 3

    class BadResponse:
        @property
        def text(self) -> str:
            raise ValueError("boom")

    bad_response = cast(httpx.Response, BadResponse())
    assert client._response_snippet(bad_response) is None


def test_next_link_parses_next_and_defaults() -> None:
    client = StacClient(
        base_url="https://example.com/stac/",
        collection="collection",
        timeout_seconds=1,
    )
    next_url, method, payload = client._next_link(
        {
            "links": [
                {"rel": "prev", "href": "https://example.com/prev"},
                {
                    "rel": "next",
                    "href": "https://example.com/next",
                    "method": "post",
                },
            ]
        }
    )
    assert next_url == "https://example.com/next"
    assert method == "POST"
    assert payload is None
    assert client._next_link({"links": "nope"}) == (None, "GET", None)


def test_next_link_ignores_invalid_entries() -> None:
    client = StacClient(
        base_url="https://example.com/stac/",
        collection="collection",
        timeout_seconds=1,
    )
    next_url, method, payload = client._next_link(
        {
            "links": [
                "bad",
                {"rel": "next"},
                {"rel": "next", "href": ""},
            ]
        }
    )
    assert next_url is None
    assert method == "GET"
    assert payload is None


def test_request_handles_status_and_request_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = StacClient(
        base_url="https://example.com/stac/",
        collection="collection",
        timeout_seconds=1,
    )

    def raise_status(*_args: object, **_kwargs: object) -> httpx.Response:
        request = httpx.Request("GET", "https://example.com")
        response = httpx.Response(500, request=request, text="boom")
        raise httpx.HTTPStatusError("boom", request=request, response=response)

    monkeypatch.setattr(client._http, "request", raise_status)
    with pytest.raises(StacUpstreamError) as exc:
        client._request("GET", "https://example.com")
    assert exc.value.status_code == 500
    assert exc.value.retryable is True

    def raise_request(*_args: object, **_kwargs: object) -> httpx.Response:
        request = httpx.Request("GET", "https://example.com")
        raise httpx.RequestError("boom", request=request)

    monkeypatch.setattr(client._http, "request", raise_request)
    with pytest.raises(StacUpstreamError) as exc_request:
        client._request("GET", "https://example.com")
    assert exc_request.value.retryable is True


def test_request_returns_response_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = StacClient(
        base_url="https://example.com/stac/",
        collection="collection",
        timeout_seconds=1,
    )
    response = httpx.Response(
        200,
        request=httpx.Request("GET", "https://example.com"),
    )

    def ok_request(*_args: object, **_kwargs: object) -> httpx.Response:
        return response

    monkeypatch.setattr(client._http, "request", ok_request)
    assert client._request("GET", "https://example.com") is response


def test_stac_client_imports_without_rasterio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import_module = importlib.import_module

    def blocked(name: str, package: str | None = None) -> object:
        if name == "rasterio" or name.startswith("rasterio."):
            raise ModuleNotFoundError("No module named 'rasterio'")
        return original_import_module(name, package)

    monkeypatch.setattr(importlib, "import_module", blocked)
    import sys

    sys.modules.pop("ndvi.stac_client", None)
    module = importlib.import_module("ndvi.stac_client")
    assert module is not None


def test_load_ndvi_array_requires_rasterio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import_module = importlib.import_module

    def blocked(name: str, package: str | None = None) -> object:
        if name == "rasterio" or name.startswith("rasterio."):
            raise ModuleNotFoundError("No module named 'rasterio'")
        return original_import_module(name, package)

    monkeypatch.setattr(importlib, "import_module", blocked)
    stac_module._require_rasterio.cache_clear()

    with pytest.raises(StacDependencyError, match="Install rasterio"):
        load_ndvi_array(
            red_href="s3://example/red.tif",
            nir_href="s3://example/nir.tif",
            bbox=BBox(
                south=Decimal("0.0"),
                west=Decimal("0.0"),
                north=Decimal("0.1"),
                east=Decimal("0.1"),
            ),
            size=128,
            timeout_seconds=1.0,
        )


def test_load_ndvi_array_rasterio_error_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRasterioError(Exception):
        pass

    class FakeEnv:
        def __init__(self, **_kwargs: object) -> None:
            self.kwargs = _kwargs

        def __enter__(self) -> FakeEnv:
            return self

        def __exit__(
            self,
            _exc_type: object,
            _exc: object,
            _tb: object,
        ) -> None:
            return None

    def fake_open(_path: str) -> None:
        raise FakeRasterioError("boom")

    fake_rasterio = SimpleNamespace(Env=FakeEnv, open=fake_open)
    monkeypatch.setattr(
        stac_module,
        "_require_rasterio",
        lambda: (
            fake_rasterio,
            SimpleNamespace(bilinear=object()),
            FakeRasterioError,
            lambda *_args, **_kwargs: (0.0, 0.0, 1.0, 1.0),
            lambda *_args, **_kwargs: object(),
        ),
    )

    with pytest.raises(StacProcessingError):
        load_ndvi_array(
            red_href="red.tif",
            nir_href="nir.tif",
            bbox=BBox(
                south=Decimal("0.0"),
                west=Decimal("0.0"),
                north=Decimal("0.1"),
                east=Decimal("0.1"),
            ),
            size=2,
            timeout_seconds=1,
        )


def test_load_ndvi_array_computes_ndvi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRasterioError(Exception):
        pass

    class FakeEnv:
        def __init__(self, **_kwargs: object) -> None:
            self.kwargs = _kwargs

        def __enter__(self) -> FakeEnv:
            return self

        def __exit__(
            self,
            _exc_type: object,
            _exc: object,
            _tb: object,
        ) -> None:
            return None

    class FakeDataset:
        def __init__(self, data: np.ndarray, *, with_crs: bool = True) -> None:
            self.crs = "EPSG:32633" if with_crs else None
            self.transform = object()
            self._data = np.ma.array(data)

        def read(
            self,
            _index: int,
            *,
            window: object,
            out_shape: object | None,
            resampling: object,
            masked: bool,
        ) -> np.ma.MaskedArray:
            return self._data

        def __enter__(self) -> FakeDataset:
            return self

        def __exit__(
            self,
            _exc_type: object,
            _exc: object,
            _tb: object,
        ) -> None:
            return None

    red_ds = FakeDataset(np.array([[1.0, 2.0]], dtype=np.float32))
    nir_ds = FakeDataset(np.array([[2.0, 4.0]], dtype=np.float32))
    datasets = [red_ds, nir_ds]

    def fake_open(_path: str) -> FakeDataset:
        return datasets.pop(0)

    fake_rasterio = SimpleNamespace(Env=FakeEnv, open=fake_open)
    monkeypatch.setattr(
        stac_module,
        "_require_rasterio",
        lambda: (
            fake_rasterio,
            SimpleNamespace(bilinear=object()),
            FakeRasterioError,
            lambda *_args, **_kwargs: (0.0, 0.0, 1.0, 1.0),
            lambda *_args, **_kwargs: object(),
        ),
    )

    ndvi = load_ndvi_array(
        red_href="red.tif",
        nir_href="nir.tif",
        bbox=BBox(
            south=Decimal("0.0"),
            west=Decimal("0.0"),
            north=Decimal("0.1"),
            east=Decimal("0.1"),
        ),
        size=2,
        timeout_seconds=1,
    )
    assert ndvi.shape == (1, 2)
    assert np.isfinite(ndvi).all()


def test_load_ndvi_array_missing_crs_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRasterioError(Exception):
        pass

    class FakeEnv:
        def __init__(self, **_kwargs: object) -> None:
            self.kwargs = _kwargs

        def __enter__(self) -> FakeEnv:
            return self

        def __exit__(
            self,
            _exc_type: object,
            _exc: object,
            _tb: object,
        ) -> None:
            return None

    class FakeDataset:
        def __init__(self, *, with_crs: bool) -> None:
            self.crs = "EPSG:32633" if with_crs else None
            self.transform = object()

        def read(
            self,
            _index: int,
            *,
            window: object,
            out_shape: object | None,
            resampling: object,
            masked: bool,
        ) -> np.ma.MaskedArray:
            return np.ma.array([[1.0]], dtype=np.float32)

        def __enter__(self) -> FakeDataset:
            return self

        def __exit__(
            self,
            _exc_type: object,
            _exc: object,
            _tb: object,
        ) -> None:
            return None

    datasets = [FakeDataset(with_crs=False), FakeDataset(with_crs=True)]

    def fake_open(_path: str) -> FakeDataset:
        return datasets.pop(0)

    fake_rasterio = SimpleNamespace(Env=FakeEnv, open=fake_open)
    monkeypatch.setattr(
        stac_module,
        "_require_rasterio",
        lambda: (
            fake_rasterio,
            SimpleNamespace(bilinear=object()),
            FakeRasterioError,
            lambda *_args, **_kwargs: (0.0, 0.0, 1.0, 1.0),
            lambda *_args, **_kwargs: object(),
        ),
    )

    with pytest.raises(StacProcessingError):
        load_ndvi_array(
            red_href="red.tif",
            nir_href="nir.tif",
            bbox=BBox(
                south=Decimal("0.0"),
                west=Decimal("0.0"),
                north=Decimal("0.1"),
                east=Decimal("0.1"),
            ),
            size=2,
            timeout_seconds=1,
        )
