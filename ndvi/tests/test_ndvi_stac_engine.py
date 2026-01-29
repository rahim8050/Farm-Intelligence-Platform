from __future__ import annotations

# ruff: noqa: S101
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast

import numpy as np
import pytest
from django.test import override_settings

import ndvi.engines.stac as stac_engine_module
from ndvi.engines.base import BBox
from ndvi.engines.stac import StacEngine, get_default_max_cloud
from ndvi.stac_client import NdviStats, StacClient, StacItem


class FakeClient:
    def __init__(self, items: list[StacItem]) -> None:
        self._items = items

    def search(
        self,
        *,
        bbox: BBox,
        start: date,
        end: date,
        max_cloud: int,
    ) -> list[StacItem]:
        return list(self._items)


def _bbox() -> BBox:
    return BBox(
        south=Decimal("0.0"),
        west=Decimal("0.0"),
        north=Decimal("0.1"),
        east=Decimal("0.1"),
    )


def _item(item_date: date) -> StacItem:
    return StacItem(
        id="item-1",
        datetime=datetime(
            item_date.year,
            item_date.month,
            item_date.day,
            tzinfo=UTC,
        ),
        assets={"B04": "red.tif", "B08": "nir.tif"},
        cloud_cover=5.0,
    )


@override_settings(NDVI_STAC_MAX_CLOUD_DEFAULT=42)
def test_get_default_max_cloud_uses_settings() -> None:
    assert get_default_max_cloud() == 42


def test_stac_engine_timeseries_caches_stats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = [_item(date(2025, 1, 2))]
    engine = StacEngine(
        client=cast(StacClient, FakeClient(items)),
        date_window_days=2,
    )
    calls: list[str] = []

    def fake_compute_stats(item: StacItem, bbox: BBox) -> NdviStats:
        calls.append(item.id)
        return NdviStats(mean=0.2, min=0.1, max=0.3, sample_count=4)

    monkeypatch.setattr(engine, "_compute_stats", fake_compute_stats)
    points = engine.get_timeseries(
        bbox=_bbox(),
        start=date(2025, 1, 1),
        end=date(2025, 1, 3),
        step_days=2,
        max_cloud=20,
    )
    assert len(points) == 2
    assert calls == ["item-1"]


def test_stac_engine_get_timeseries_returns_empty_when_no_items() -> None:
    engine = StacEngine(client=cast(StacClient, FakeClient([])))
    points = engine.get_timeseries(
        bbox=_bbox(),
        start=date(2025, 1, 1),
        end=date(2025, 1, 3),
        step_days=2,
        max_cloud=20,
    )
    assert points == []


def test_stac_engine_get_timeseries_skips_when_stats_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = [_item(date(2025, 1, 2))]
    engine = StacEngine(client=cast(StacClient, FakeClient(items)))
    monkeypatch.setattr(engine, "_compute_stats", lambda *_: None)
    points = engine.get_timeseries(
        bbox=_bbox(),
        start=date(2025, 1, 1),
        end=date(2025, 1, 3),
        step_days=2,
        max_cloud=20,
    )
    assert points == []


def test_stac_engine_get_latest_returns_none_when_missing_item() -> None:
    engine = StacEngine(client=cast(StacClient, FakeClient([])))
    assert (
        engine.get_latest(
            bbox=_bbox(),
            lookback_days=7,
            max_cloud=20,
        )
        is None
    )


def test_stac_engine_get_latest_returns_none_when_stats_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = [_item(date.today())]
    engine = StacEngine(client=cast(StacClient, FakeClient(items)))
    monkeypatch.setattr(engine, "_compute_stats", lambda *_: None)
    assert (
        engine.get_latest(
            bbox=_bbox(),
            lookback_days=7,
            max_cloud=20,
        )
        is None
    )


def test_stac_engine_get_latest_returns_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = [_item(date.today())]
    engine = StacEngine(client=cast(StacClient, FakeClient(items)))

    def fake_compute_stats(_item: StacItem, _bbox: BBox) -> NdviStats:
        return NdviStats(mean=0.2, min=0.1, max=0.3, sample_count=4)

    monkeypatch.setattr(engine, "_compute_stats", fake_compute_stats)
    point = engine.get_latest(
        bbox=_bbox(),
        lookback_days=7,
        max_cloud=20,
    )
    assert point is not None
    assert point.mean == 0.2


def test_stac_engine_compute_stats_missing_assets_returns_none() -> None:
    engine = StacEngine(client=cast(StacClient, FakeClient([])))
    item = StacItem(
        id="missing",
        datetime=datetime(2025, 1, 2, tzinfo=UTC),
        assets={"B04": "red.tif"},
        cloud_cover=5.0,
    )
    assert engine._compute_stats(item, _bbox()) is None


def test_stac_engine_compute_stats_returns_stats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = StacEngine(client=cast(StacClient, FakeClient([])))

    def fake_load_ndvi_array(*_args: object, **_kwargs: object) -> np.ndarray:
        return np.array([[0.2]], dtype=np.float32)

    def fake_compute_ndvi_stats(_ndvi: np.ndarray) -> NdviStats:
        return NdviStats(mean=0.2, min=0.1, max=0.3, sample_count=1)

    monkeypatch.setattr(
        stac_engine_module, "load_ndvi_array", fake_load_ndvi_array
    )
    monkeypatch.setattr(
        stac_engine_module, "compute_ndvi_stats", fake_compute_ndvi_stats
    )

    item = _item(date(2025, 1, 2))
    stats = engine._compute_stats(item, _bbox())
    assert stats is not None
    assert stats.sample_count == 1
