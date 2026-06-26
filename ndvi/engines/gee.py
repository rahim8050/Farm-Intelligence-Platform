"""Google Earth Engine NDVI engine adapter.

Provides NDVI from Google Earth Engine as a batch/backfill or
on-demand engine. Uses STAC to discover and process Sentinel-2
imagery, independently configurable from the ``stac`` engine.

Configure via ``NDVI_GEE_STAC_API_URL`` and ``NDVI_GEE_STAC_COLLECTION``
env vars. Defaults to the Copernicus Data Space Ecosystem STAC API
(no API key required for read access).
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Final

import numpy as np
from django.conf import settings

from ndvi.engines.base import BBox, NDVIEngine, NdviPoint
from ndvi.stac_client import (
    DEFAULT_STATS_SAMPLE_SIZE,
    NdviStats,
    StacClient,
    build_asset_candidates,
    compute_ndvi_stats,
    load_ndvi_array,
    load_ndwi_array,
    resolve_asset_href_candidates,
    select_best_item,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0
DEFAULT_DATE_WINDOW_DAYS: Final[int] = 3
DEFAULT_MAX_CLOUD: Final[int] = 30
DEFAULT_INDEX_TYPE: Final[str] = "NDVI"
DEFAULT_ASSET_RED: Final[str] = "B04_10m"
DEFAULT_ASSET_GREEN: Final[str] = "B03_10m"
DEFAULT_ASSET_NIR: Final[str] = "B08_10m"
DEFAULT_ASSET_SCL: Final[str] = "SCL"
DEFAULT_MASK_WATER: Final[bool] = False


def _load_gee_ndvi(engine: GeeEngine, item: Any, bbox: BBox) -> np.ndarray:
    nir_href = resolve_asset_href_candidates(
        item,
        build_asset_candidates(engine.asset_nir),
    )
    red_href = resolve_asset_href_candidates(
        item,
        build_asset_candidates(engine.asset_red),
    )
    scl_href = resolve_asset_href_candidates(
        item,
        build_asset_candidates(engine.asset_scl),
    )
    if not red_href or not nir_href:
        logger.warning(
            "gee.item.missing_assets item_id=%s",
            getattr(item, "id", "-"),
        )
        return np.array([])
    return load_ndvi_array(
        red_href=red_href,
        nir_href=nir_href,
        bbox=bbox,
        size=DEFAULT_STATS_SAMPLE_SIZE,
        timeout_seconds=engine.timeout_seconds,
        scl_href=scl_href,
        mask_water=engine.mask_water,
    )


def _load_gee_ndwi(engine: GeeEngine, item: Any, bbox: BBox) -> np.ndarray:
    nir_href = resolve_asset_href_candidates(
        item,
        build_asset_candidates(engine.asset_nir),
    )
    green_href = resolve_asset_href_candidates(
        item,
        build_asset_candidates(engine.asset_green),
    )
    scl_href = resolve_asset_href_candidates(
        item,
        build_asset_candidates(engine.asset_scl),
    )
    if not green_href or not nir_href:
        logger.warning(
            "gee.item.missing_assets item_id=%s",
            getattr(item, "id", "-"),
        )
        return np.array([])
    return load_ndwi_array(
        green_href=green_href,
        nir_href=nir_href,
        bbox=bbox,
        size=DEFAULT_STATS_SAMPLE_SIZE,
        timeout_seconds=engine.timeout_seconds,
        scl_href=scl_href,
        mask_water=engine.mask_water,
    )


_INDEX_LOADERS: Final[dict[str, Any]] = {
    "NDVI": _load_gee_ndvi,
    "NDWI": _load_gee_ndwi,
}


def _str_setting(name: str, default: str) -> str:
    return str(getattr(settings, f"NDVI_GEE_{name}", default))


def _int_setting(name: str, default: int) -> int:
    return int(getattr(settings, f"NDVI_GEE_{name}", default))


def _float_setting(name: str, default: float) -> float:
    return float(getattr(settings, f"NDVI_GEE_{name}", default))


def _bool_setting(name: str, default: bool) -> bool:
    return bool(getattr(settings, f"NDVI_GEE_{name}", default))


class GeeEngine(NDVIEngine):
    """NDVI engine backed by a configurable STAC API.

    Independently configurable from the ``stac`` engine. Defaults to
    the Copernicus Data Space Ecosystem STAC endpoint with Sentinel-2
    L2A collection.
    """

    engine_name: str = "gee"

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
        asset_scl: str | None = None,
        mask_water: bool | None = None,
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
        self.asset_scl = asset_scl or _str_setting(
            "ASSET_SCL", DEFAULT_ASSET_SCL
        )
        self.mask_water = (
            mask_water
            if mask_water is not None
            else _bool_setting("MASK_WATER", DEFAULT_MASK_WATER)
        )
        self.client = client or StacClient(
            base_url=_str_setting(
                "STAC_API_URL",
                "https://stac.dataspace.copernicus.eu/v1/",
            ),
            collection=_str_setting("STAC_COLLECTION", "sentinel-2-l2a"),
            timeout_seconds=self.timeout_seconds,
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
        window = timedelta(days=self.date_window_days)
        items = self.client.search(
            bbox=bbox,
            start=start - window,
            end=end + window,
            max_cloud=cloud,
        )
        points: list[NdviPoint] = []
        stats_cache: dict[str, tuple[NdviPoint, object]] = {}

        for bucket_date in self._iter_buckets(start, end, step_days):
            item = select_best_item(
                items,
                target_date=bucket_date,
                window_days=self.date_window_days,
            )
            if not item:
                continue
            cached = stats_cache.get(item.id)
            if cached:
                cached_point, _ = cached
                points.append(cached_point)
                continue
            stats = self._compute_stats(item, bbox)
            if not stats:
                continue
            # Reuse StacEngine's normalization logic via module import
            # to avoid duplicating cloud/SCL handling.
            point = NdviPoint(
                date=bucket_date,
                mean=stats.mean,
                min=stats.min,
                max=stats.max,
                sample_count=stats.sample_count,
                cloud_fraction=stats.quality_flags.get("cloud_fraction")
                if stats.quality_flags
                else None,
                valid_pixel_fraction=stats.valid_pixel_fraction,
                quality_flags=stats.quality_flags,
            )
            stats_cache[item.id] = (point, None)
            points.append(point)
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
        today = date.today()
        start = today - timedelta(days=lookback_days)
        items = self.client.search(
            bbox=bbox,
            start=start,
            end=today,
            max_cloud=cloud,
        )
        item = select_best_item(
            items,
            target_date=today,
            window_days=lookback_days,
        )
        if not item:
            return None
        stats = self._compute_stats(item, bbox)
        if not stats:
            return None
        return NdviPoint(
            date=item.date,
            mean=stats.mean,
            min=stats.min,
            max=stats.max,
            sample_count=stats.sample_count,
            cloud_fraction=getattr(item, "cloud_cover", None),
            valid_pixel_fraction=stats.valid_pixel_fraction,
            quality_flags=stats.quality_flags,
        )

    def _iter_buckets(
        self, start: date, end: date, step_days: int
    ) -> list[date]:
        buckets: list[date] = []
        cursor = start
        while cursor <= end:
            buckets.append(cursor)
            cursor = cursor + timedelta(days=step_days)
        return buckets

    def _compute_stats(self, item: Any, bbox: BBox) -> NdviStats | None:
        index_loader = _INDEX_LOADERS.get(self.index_type)
        if index_loader is None:
            raise ValueError(f"Unsupported index type: {self.index_type}")
        index_array = index_loader(self, item, bbox)
        if index_array.size == 0:
            return None

        stats = compute_ndvi_stats(index_array)
        if stats is None:
            return None

        valid_pixel_fraction = stats.valid_pixel_fraction
        quality_flags = stats.quality_flags

        return NdviStats(
            mean=stats.mean,
            min=stats.min,
            max=stats.max,
            sample_count=stats.sample_count,
            valid_pixel_fraction=valid_pixel_fraction,
            quality_flags=quality_flags,
        )
