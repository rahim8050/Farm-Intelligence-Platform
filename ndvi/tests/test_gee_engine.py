"""Tests for the GEE engine adapter (STAC-based)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from ndvi.engines.base import BBox
from ndvi.engines.gee import GeeEngine


class _MockStacClient:
    """Minimal mock that returns no STAC items."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.collection = str(kwargs.get("collection", ""))

    def search(self, *args: object, **kwargs: object) -> list[object]:
        return []


def _bbox() -> BBox:
    return BBox(
        south=Decimal(0),
        west=Decimal(0),
        north=Decimal(1),
        east=Decimal(1),
    )


class TestGeeEngine:
    """GeeEngine is STAC-based, returning results from remote APIs."""

    def setup_method(self) -> None:
        self.engine = GeeEngine(
            client=_MockStacClient(collection="sentinel-2-l2a"),  # type: ignore[arg-type]
        )

    def test_timeseries_returns_empty_list(self) -> None:
        result = self.engine.get_timeseries(
            bbox=_bbox(),
            start=date(2025, 1, 1),
            end=date(2025, 1, 10),
            step_days=5,
            max_cloud=50,
        )
        assert result == []

    def test_latest_returns_none(self) -> None:
        result = self.engine.get_latest(
            bbox=_bbox(),
            lookback_days=30,
            max_cloud=50,
        )
        assert result is None

    def test_default_collection(self) -> None:
        assert self.engine.client.collection == "sentinel-2-l2a"
