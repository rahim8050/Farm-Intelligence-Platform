"""Engine abstractions for NDVI providers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class BBox:
    """Normalized bounding box for NDVI requests (WGS84 decimal degrees)."""

    south: Decimal
    west: Decimal
    north: Decimal
    east: Decimal


@dataclass(frozen=True)
class NdviPoint:
    """Single NDVI observation for a time bucket."""

    date: date
    mean: float
    min: float | None = None
    max: float | None = None
    sample_count: int | None = None
    cloud_fraction: float | None = None
    valid_pixel_fraction: float | None = None
    quality_flags: dict[str, bool] | None = None


class NDVIEngine(Protocol):
    """Interface for NDVI engines capable of producing time series data."""

    def get_timeseries(
        self,
        *,
        bbox: BBox,
        start: date,
        end: date,
        step_days: int,
        max_cloud: int,
    ) -> list[NdviPoint]:
        """Return NDVI points over a date range."""

    def get_latest(
        self,
        *,
        bbox: BBox,
        lookback_days: int,
        max_cloud: int,
    ) -> NdviPoint | None:
        """Return the most recent NDVI point within the lookback window."""
