"""Generic spectral compute engine.

A single engine class that can compute any spectral index from
any data provider.  Parameterised by a ``DataProvider`` instance
and an ``IndexDefinition`` from ``FORMULA_REGISTRY``.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

import numpy as np

from ndvi.engines.base import BBox, NDVIEngine, NdviPoint
from ndvi.providers.base import DataProvider
from ndvi.stac_client import (
    compute_ndvi_stats,
    normalize_cloud_fraction,
    select_best_item,
)
from science.formulas.band_registry import BAND_REGISTRY, get_band_asset_key
from science.formulas.registry import IndexDefinition

logger = logging.getLogger(__name__)


class SpectralComputeEngine(NDVIEngine):
    """One engine to compute any spectral index from any provider.

    No ``if index_type`` branches — the formula and band mappings
    are read from ``FORMULA_REGISTRY`` and ``BAND_REGISTRY``
    respectively.

    Args:
        provider: DataProvider instance (e.g. ``StacDataProvider``).
        formula: IndexDefinition from ``FORMULA_REGISTRY``.
    """

    engine_name: str = "spectral"
    index_type: str = "NDVI"

    def __init__(
        self,
        *,
        provider: DataProvider,
        formula: IndexDefinition,
    ) -> None:
        self.provider = provider
        self.formula = formula
        self.index_type = formula["name"]
        self.engine_name = f"spectral_{provider.sensor_key}"
        self._band_map = BAND_REGISTRY.get(provider.sensor_key, {})

    def compute(
        self,
        *,
        bbox: BBox,
        start: date,
        end: date,
        step_days: int,
        max_cloud: int,
    ) -> list[NdviPoint]:
        """Compute spectral index over a date range.

        Steps:
            1. Search for items via the provider.
            2. For each time bucket, select the best item.
            3. Load the required bands for the formula.
            4. Compute the index using the formula's callable.
            5. Compute and return statistics.

        Returns:
            List of NdviPoint, one per bucket date.
        """
        window = timedelta(days=5)
        search_start = start - window
        search_end = end + window

        items = self.provider.search(
            bbox=bbox,
            start=search_start,
            end=search_end,
            max_cloud=max_cloud,
        )

        points: list[NdviPoint] = []
        for bucket_date in self._iter_buckets(start, end, step_days):
            item = select_best_item(
                items,
                target_date=bucket_date,
                window_days=5,
            )
            if not item:
                continue

            point = self._compute_for_item(item, bbox, bucket_date)
            if point is not None:
                points.append(point)

        return points

    def get_timeseries(
        self,
        *,
        bbox: BBox,
        start: date,
        end: date,
        step_days: int,
        max_cloud: int,
    ) -> list[NdviPoint]:
        """Compute spectral index over a date range (NDVIEngine interface)."""
        return self.compute(
            bbox=bbox,
            start=start,
            end=end,
            step_days=step_days,
            max_cloud=max_cloud,
        )

    def get_latest(
        self,
        *,
        bbox: BBox,
        lookback_days: int,
        max_cloud: int,
    ) -> NdviPoint | None:
        """Return the most recent spectral index value."""
        item = self.provider.get_latest(
            bbox=bbox,
            lookback_days=lookback_days,
            max_cloud=max_cloud,
        )
        if not item:
            return None
        return self._compute_for_item(
            item,
            bbox,
            bucket_date=item.date,
        )

    def _iter_buckets(
        self,
        start: date,
        end: date,
        step_days: int,
    ) -> list[date]:
        buckets: list[date] = []
        cursor = start
        while cursor <= end:
            buckets.append(cursor)
            cursor += timedelta(days=step_days)
        return buckets

    def _compute_for_item(
        self,
        item: Any,
        bbox: BBox,
        bucket_date: date,
    ) -> NdviPoint | None:
        """Load bands, compute index, and return stats for a single item."""
        index_array = self._compute_index_array(item, bbox)
        if index_array is None:
            return None

        stats = compute_ndvi_stats(index_array)
        if stats is None:
            return None

        return NdviPoint(
            date=bucket_date,
            mean=stats.mean,
            min=stats.min,
            max=stats.max,
            sample_count=stats.sample_count,
            cloud_fraction=normalize_cloud_fraction(
                getattr(item, "cloud_cover", None)
            ),
            valid_pixel_fraction=stats.valid_pixel_fraction,
            quality_flags=stats.quality_flags,
        )

    def _load_band_arrays(
        self,
        item: Any,
        bbox: BBox,
    ) -> dict[str, np.ndarray] | None:
        required_bands = self.formula["bands"]
        band_arrays: dict[str, np.ndarray] = {}

        for band_name in required_bands:
            try:
                asset_key = get_band_asset_key(
                    self.provider.sensor_key, band_name
                )
            except KeyError:
                logger.warning(
                    "band_registry.missing sensor=%s band=%s",
                    self.provider.sensor_key,
                    band_name,
                )
                return None

            arr = self.provider.load_band(item, asset_key, bbox)
            if arr.size == 0:
                logger.warning(
                    "spectral.band_empty item_id=%s band=%s",
                    getattr(item, "id", "-"),
                    band_name,
                )
                return None
            band_arrays[band_name] = arr

        return band_arrays

    def _compute_index_array(
        self,
        item: Any,
        bbox: BBox,
    ) -> np.ndarray | None:
        band_arrays = self._load_band_arrays(item, bbox)
        if band_arrays is None:
            return None
        with np.errstate(divide="ignore", invalid="ignore"):
            return self.formula["formula"](**band_arrays)
