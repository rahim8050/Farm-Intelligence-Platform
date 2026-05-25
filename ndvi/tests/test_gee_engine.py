"""Tests for the GEE engine adapter (stub)."""

from __future__ import annotations

from datetime import date

from ndvi.engines.base import BBox
from ndvi.engines.gee import GeeEngine


def _bbox() -> BBox:
    return BBox(south=0, west=0, north=1, east=1)


class TestGeeEngine:
    """GeeEngine is a stub returning empty/null results."""

    def setup_method(self) -> None:
        self.engine = GeeEngine()

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

    def test_custom_collection(self) -> None:
        engine = GeeEngine(collection="LANDSAT/LC08/C02/T1_L2")
        assert engine.collection == "LANDSAT/LC08/C02/T1_L2"

    def test_default_collection(self) -> None:
        assert self.engine.collection == "COPERNICUS/S2_SR_HARMONIZED"
