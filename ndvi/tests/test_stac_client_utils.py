from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal
from typing import cast

import pytest

from ndvi.engines.base import BBox
from ndvi.stac_client import (
    StacItem,
    _coerce_bbox_values,
    _looks_like_lat_lon_order,
    _validate_stac_bbox_values,
    build_asset_fallbacks,
    filter_items_by_cloud,
    normalize_stac_bbox,
    select_best_item,
)


def _make_item(
    *,
    id: str,
    dt: datetime,
    cloud: float | None,
) -> StacItem:
    return StacItem(
        id=id,
        datetime=dt,
        assets={"data": "url"},
        cloud_cover=cloud,
        collection="sentinel",
    )


def test_validate_bbox_rejects_bad_values() -> None:
    assert not _validate_stac_bbox_values((math.inf, 0.0, 10.0, 10.0))
    assert not _validate_stac_bbox_values((0.0, -100.0, 10.0, 10.0))
    assert not _validate_stac_bbox_values((0.0, 0.0, 0.0, 0.0))


def test_looks_like_lat_lon_order_true() -> None:
    assert _looks_like_lat_lon_order((45.0, -170.0, 46.0, -169.0))
    assert not _looks_like_lat_lon_order((-170.0, 45.0, -169.0, 46.0))


def test_coerce_handles_bbox_dataclass() -> None:
    bbox = BBox(
        south=Decimal("-2.0"),
        west=Decimal("-1.0"),
        north=Decimal("2.0"),
        east=Decimal("1.0"),
    )
    assert _coerce_bbox_values(bbox) == (-1.0, -2.0, 1.0, 2.0)


def test_coerce_raises_for_bad_length() -> None:
    with pytest.raises(ValueError):
        _coerce_bbox_values(cast(list[float], [0.0, 0.0, 0.0]))


def test_normalize_swaps_lat_lon(caplog: pytest.LogCaptureFixture) -> None:
    lat_lon = (40.0, -100.0, 41.0, -99.0)
    caplog.set_level("WARNING")
    result = normalize_stac_bbox(
        lat_lon,
        job_id=1,
        farm_id=2,
        log_on_swap=True,
    )
    assert result == (-100.0, 40.0, -99.0, 41.0)
    assert "bbox_swapped" in caplog.text


def test_normalize_raises_for_invalid() -> None:
    with pytest.raises(ValueError):
        normalize_stac_bbox((0.0, 0.0, 0.0, 0.0))


def test_build_asset_fallbacks_excludes_empty_normalizes() -> None:
    assert build_asset_fallbacks("  B04_10m  ") == [
        "B04_20m",
        "B04_60m",
        "B04",
    ]
    assert build_asset_fallbacks("") == []


def test_filter_and_select_best_item() -> None:
    items = [
        _make_item(id="a", dt=datetime(2023, 1, 1), cloud=20.0),
        _make_item(id="b", dt=datetime(2023, 1, 2), cloud=None),
        _make_item(id="c", dt=datetime(2023, 1, 3), cloud=120.0),
    ]
    filtered = filter_items_by_cloud(items, max_cloud=50)
    assert {item.id for item in filtered} == {"a", "b"}
    best = select_best_item(
        filtered,
        target_date=date(2023, 1, 2),
        window_days=2,
    )
    assert best is not None
    assert best.id == "a"
