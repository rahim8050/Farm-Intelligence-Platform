"""Landsat Collection 2 SR NDVI engine adapter.

Provides NDVI from Landsat 8/9 Surface Reflectance as a fallback
when Sentinel-2 is unavailable or unreliable.

Uses STAC (default: Microsoft Planetary Computer) to discover and
process Landsat scenes. Configurable via ``NDVI_LANDSAT_*`` env vars.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Final

import numpy as np
from django.conf import settings

from ndvi.engines.base import BBox, NDVIEngine, NdviPoint
from ndvi.engines.compute import SpectralComputeEngine
from ndvi.metrics import spectral_shadow_comparison_diffs_total
from ndvi.providers.stac import StacDataProvider, StacItem
from ndvi.stac_client import (
    NdviStats,
    StacClient,
    compute_ndvi_stats,
    normalize_cloud_fraction,
    select_best_item,
)
from science.formulas.registry import FORMULA_REGISTRY

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0
DEFAULT_DATE_WINDOW_DAYS: Final[int] = 5
DEFAULT_MAX_CLOUD: Final[int] = 30
DEFAULT_INDEX_TYPE: Final[str] = "NDVI"
DEFAULT_ASSET_RED: Final[str] = "B4"
DEFAULT_ASSET_GREEN: Final[str] = "B3"
DEFAULT_ASSET_NIR: Final[str] = "B5"


def _str_setting(name: str, default: str) -> str:
    return str(getattr(settings, f"NDVI_LANDSAT_{name}", default))


def _int_setting(name: str, default: int) -> int:
    return int(getattr(settings, f"NDVI_LANDSAT_{name}", default))


def _float_setting(name: str, default: float) -> float:
    return float(getattr(settings, f"NDVI_LANDSAT_{name}", default))


class LandsatDataProvider(StacDataProvider):
    """STAC DataProvider specialized for Landsat asset key overrides."""

    def __init__(
        self,
        *,
        client: StacClient,
        sensor_key: str = "landsat89_l2",
        timeout_seconds: float = 30.0,
        asset_red: str,
        asset_green: str,
        asset_nir: str,
    ) -> None:
        super().__init__(
            client=client,
            sensor_key=sensor_key,
            timeout_seconds=timeout_seconds,
        )
        self.asset_red = asset_red
        self.asset_green = asset_green
        self.asset_nir = asset_nir

    def load_band(
        self,
        item: StacItem,
        band_asset_key: str,
        bbox: BBox,
    ) -> np.ndarray:
        # Override the defaults from BAND_REGISTRY with instance overrides
        if band_asset_key == "B4":
            band_asset_key = self.asset_red
        elif band_asset_key == "B3":
            band_asset_key = self.asset_green
        elif band_asset_key == "B5":
            band_asset_key = self.asset_nir
        return super().load_band(item, band_asset_key, bbox)


class LandsatEngine(NDVIEngine):
    """NDVI engine backed by Landsat Collection 2 via STAC.

    Defaults to Microsoft Planetary Computer STAC API with Landsat
    8/9 Collection 2 Level-2 collections.
    """

    engine_name: str = "landsat"

    def __init__(
        self,
        *,
        client: StacClient | None = None,
        timeout_seconds: float | None = None,
        date_window_days: int | None = None,
        index_type: str = "NDVI",
        asset_red: str | None = None,
        asset_green: str | None = None,
        asset_nir: str | None = None,
    ) -> None:
        self.index_type = index_type
        self.timeout_seconds = timeout_seconds or _float_setting(
            "TIMEOUT_SECS", DEFAULT_TIMEOUT_SECONDS
        )
        self.date_window_days = date_window_days or _int_setting(
            "DATE_WINDOW_DAYS", DEFAULT_DATE_WINDOW_DAYS
        )
        self.asset_red = asset_red or _str_setting(
            "ASSET_RED", DEFAULT_ASSET_RED
        )
        self.asset_green = asset_green or _str_setting(
            "ASSET_GREEN", DEFAULT_ASSET_GREEN
        )
        self.asset_nir = asset_nir or _str_setting(
            "ASSET_NIR", DEFAULT_ASSET_NIR
        )
        self.client = client or StacClient(
            base_url=_str_setting(
                "STAC_API_URL",
                "https://planetarycomputer.microsoft.com/api/stac/v1/",
            ),
            collection=_str_setting("STAC_COLLECTION", "landsat-8-c2-l2"),
            timeout_seconds=self.timeout_seconds,
        )

        # Build data provider and generic compute engine delegator
        self._provider = LandsatDataProvider(
            client=self.client,
            sensor_key="landsat89_l2",
            timeout_seconds=self.timeout_seconds,
            asset_red=self.asset_red,
            asset_green=self.asset_green,
            asset_nir=self.asset_nir,
        )
        formula = FORMULA_REGISTRY[index_type]
        self._delegate = SpectralComputeEngine(
            provider=self._provider,
            formula=formula,
        )

    def get_timeseries(
        self,
        *,
        bbox: BBox,
        start: date,
        end: date,
        step_days: int,
        max_cloud: int | None = None,
    ) -> list[NdviPoint]:
        cloud = (
            max_cloud
            if max_cloud is not None
            else _int_setting("MAX_CLOUD_DEFAULT", DEFAULT_MAX_CLOUD)
        )
        points = self._delegate.get_timeseries(
            bbox=bbox,
            start=start,
            end=end,
            step_days=step_days,
            max_cloud=cloud,
        )
        self._shadow_compare_timeseries(
            bbox=bbox,
            start=start,
            end=end,
            step_days=step_days,
            max_cloud=cloud,
            spectral_points=points,
        )
        return points

    def get_latest(
        self,
        *,
        bbox: BBox,
        lookback_days: int,
        max_cloud: int | None = None,
    ) -> NdviPoint | None:
        cloud = (
            max_cloud
            if max_cloud is not None
            else _int_setting("MAX_CLOUD_DEFAULT", DEFAULT_MAX_CLOUD)
        )
        point = self._delegate.get_latest(
            bbox=bbox,
            lookback_days=lookback_days,
            max_cloud=cloud,
        )
        self._shadow_compare_latest(
            bbox=bbox,
            lookback_days=lookback_days,
            max_cloud=cloud,
            spectral_point=point,
        )
        return point

    def _iter_buckets(
        self, start: date, end: date, step_days: int
    ) -> list[date]:
        return self._delegate._iter_buckets(start, end, step_days)

    def _compute_stats(self, item: Any, bbox: BBox) -> NdviStats | None:
        # Keep this for backward compatibility and unit tests.
        # Delegates to _compute_for_item
        point = self._delegate._compute_for_item(
            item,
            bbox,
            getattr(item, "date", date.today()),
        )
        if point is None:
            return None
        if (
            point.min is None
            or point.max is None
            or point.sample_count is None
        ):
            return None
        return NdviStats(
            mean=point.mean,
            min=point.min,
            max=point.max,
            sample_count=point.sample_count,
            valid_pixel_fraction=point.valid_pixel_fraction,
            quality_flags=point.quality_flags,
        )

    def _legacy_compute_point(
        self,
        item: StacItem,
        bbox: BBox,
        bucket_date: date,
    ) -> tuple[NdviPoint | None, float | None]:
        band_arrays = self._delegate._load_band_arrays(item, bbox)
        if band_arrays is None:
            return None, None

        with np.errstate(divide="ignore", invalid="ignore"):
            index_array = self._delegate.formula["formula"](**band_arrays)

        stddev = float(np.nanstd(index_array))
        stats = compute_ndvi_stats(index_array)
        if stats is None:
            return None, None

        point = NdviPoint(
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
        return point, stddev

    def _shadow_compare_timeseries(
        self,
        *,
        bbox: BBox,
        start: date,
        end: date,
        step_days: int,
        max_cloud: int,
        spectral_points: list[NdviPoint],
    ) -> None:
        if not getattr(settings, "SHADOW_COMPARE_ENABLED", False):
            return

        search_window = timedelta(days=5)
        items = self._provider.search(
            bbox=bbox,
            start=start - search_window,
            end=end + search_window,
            max_cloud=max_cloud,
        )
        legacy_points: list[tuple[NdviPoint, float]] = []
        for bucket_date in self._delegate._iter_buckets(start, end, step_days):
            item = select_best_item(
                items,
                target_date=bucket_date,
                window_days=5,
            )
            if item is None:
                continue
            legacy_point, legacy_stddev = self._legacy_compute_point(
                item,
                bbox,
                bucket_date,
            )
            if legacy_point is None or legacy_stddev is None:
                continue
            legacy_points.append((legacy_point, legacy_stddev))

        self._log_shadow_diffs(
            legacy_points=legacy_points,
            spectral_points=spectral_points,
            endpoint="timeseries",
        )

    def _shadow_compare_latest(
        self,
        *,
        bbox: BBox,
        lookback_days: int,
        max_cloud: int,
        spectral_point: NdviPoint | None,
    ) -> None:
        if not getattr(settings, "SHADOW_COMPARE_ENABLED", False):
            return

        item = self._provider.get_latest(
            bbox=bbox,
            lookback_days=lookback_days,
            max_cloud=max_cloud,
        )
        if item is None:
            return
        legacy_point, legacy_stddev = self._legacy_compute_point(
            item,
            bbox,
            item.date,
        )
        if legacy_point is None or legacy_stddev is None:
            return
        if spectral_point is None:
            return
        self._log_shadow_diffs(
            legacy_points=[(legacy_point, legacy_stddev)],
            spectral_points=[spectral_point],
            endpoint="latest",
        )

    def _log_shadow_diffs(
        self,
        *,
        legacy_points: list[tuple[NdviPoint, float]],
        spectral_points: list[NdviPoint],
        endpoint: str,
    ) -> None:
        if len(legacy_points) != len(spectral_points):
            logger.warning(
                (
                    "landsat.shadow.count_mismatch "
                    "endpoint=%s legacy=%s spectral=%s"
                ),
                endpoint,
                len(legacy_points),
                len(spectral_points),
            )
            spectral_shadow_comparison_diffs_total.labels(
                engine=self.engine_name,
                index=self.index_type,
                field="count",
            ).inc()
            return

        for (legacy_point, legacy_stddev), spectral_point in zip(
            legacy_points,
            spectral_points,
            strict=True,
        ):
            mean_delta = abs(legacy_point.mean - spectral_point.mean)
            stddev_delta = abs(legacy_stddev - 0.0)
            if mean_delta > 1e-6 or stddev_delta > 1e-6:
                logger.warning(
                    (
                        "landsat.shadow.diff endpoint=%s date=%s "
                        "mean_delta=%s stddev_delta=%s"
                    ),
                    endpoint,
                    legacy_point.date,
                    mean_delta,
                    stddev_delta,
                )
                spectral_shadow_comparison_diffs_total.labels(
                    engine=self.engine_name,
                    index=self.index_type,
                    field="mean",
                ).inc()
                spectral_shadow_comparison_diffs_total.labels(
                    engine=self.engine_name,
                    index=self.index_type,
                    field="stddev",
                ).inc()
