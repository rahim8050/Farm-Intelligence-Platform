"""STAC-based NDVI engine using COG assets."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Final

from django.conf import settings

from ndvi.engines.base import BBox, NDVIEngine, NdviPoint
from ndvi.stac_client import (
    DEFAULT_STATS_SAMPLE_SIZE,
    NdviStats,
    StacClient,
    StacItem,
    build_asset_candidates,
    compute_ndvi_stats,
    load_ndmi_array,
    load_ndvi_array,
    load_ndwi_array,
    normalize_cloud_fraction,
    resolve_asset_href_candidates,
    select_best_item,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0
DEFAULT_DATE_WINDOW_DAYS: Final[int] = 3
DEFAULT_MAX_CLOUD: Final[int] = 30
DEFAULT_INDEX_TYPE: Final[str] = "NDVI"
DEFAULT_ASSET_RED: Final[str] = "B04"
DEFAULT_ASSET_GREEN: Final[str] = "B03"
DEFAULT_ASSET_NIR: Final[str] = "B08"
DEFAULT_ASSET_SCL: Final[str] = "SCL"
DEFAULT_ASSET_SWIR1_10M: Final[str] = "B11_10m"
DEFAULT_ASSET_SWIR1_20M: Final[str] = "B11_20m"
DEFAULT_ASSET_SWIR1: Final[str] = DEFAULT_ASSET_SWIR1_20M
DEFAULT_MASK_WATER: Final[bool] = False


def get_default_timeout_seconds() -> float:
    return float(
        getattr(settings, "NDVI_STAC_TIMEOUT_SECS", DEFAULT_TIMEOUT_SECONDS)
    )


def get_default_date_window_days() -> int:
    return int(
        getattr(
            settings,
            "NDVI_STAC_DATE_WINDOW_DAYS",
            DEFAULT_DATE_WINDOW_DAYS,
        )
    )


def get_default_max_cloud() -> int:
    return int(
        getattr(settings, "NDVI_STAC_MAX_CLOUD_DEFAULT", DEFAULT_MAX_CLOUD)
    )


def get_default_asset_red() -> str:
    return str(getattr(settings, "NDVI_STAC_ASSET_RED", DEFAULT_ASSET_RED))


def get_default_asset_nir() -> str:
    return str(getattr(settings, "NDVI_STAC_ASSET_NIR", DEFAULT_ASSET_NIR))


def get_default_asset_scl() -> str:
    return str(getattr(settings, "NDVI_STAC_ASSET_SCL", DEFAULT_ASSET_SCL))


def get_default_mask_water() -> bool:
    return bool(getattr(settings, "NDVI_STAC_MASK_WATER", DEFAULT_MASK_WATER))


def get_default_asset_swir1() -> str:
    return str(getattr(settings, "NDVI_STAC_ASSET_SWIR1", DEFAULT_ASSET_SWIR1))


class StacEngine(NDVIEngine):
    """Fetch NDVI metrics from a STAC API."""

    engine_name: str = "stac"

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
        asset_swir1: str | None = None,
        asset_scl: str | None = None,
        mask_water: bool | None = None,
    ) -> None:
        self.index_type = index_type
        self.timeout_seconds = timeout_seconds or get_default_timeout_seconds()
        self.date_window_days = (
            date_window_days or get_default_date_window_days()
        )
        self.asset_red = asset_red or get_default_asset_red()
        self.asset_green = asset_green or DEFAULT_ASSET_GREEN
        self.asset_nir = asset_nir or get_default_asset_nir()
        self.asset_swir1 = asset_swir1 or get_default_asset_swir1()
        self.asset_scl = asset_scl or get_default_asset_scl()
        self.mask_water = (
            mask_water if mask_water is not None else get_default_mask_water()
        )
        self.client = client or StacClient(
            timeout_seconds=self.timeout_seconds
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
        cloud = max_cloud if max_cloud is not None else get_default_max_cloud()
        window = timedelta(days=self.date_window_days)
        search_start = start - window
        search_end = end + window
        items = self.client.search(
            bbox=bbox,
            start=search_start,
            end=search_end,
            max_cloud=cloud,
        )
        points: list[NdviPoint] = []
        stats_cache: dict[str, tuple[NdviPoint, dict[str, bool] | None]] = {}

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
                cached_point, cached_flags = cached
                points.append(
                    NdviPoint(
                        date=bucket_date,
                        mean=cached_point.mean,
                        min=cached_point.min,
                        max=cached_point.max,
                        sample_count=cached_point.sample_count,
                        cloud_fraction=cached_point.cloud_fraction,
                        valid_pixel_fraction=cached_point.valid_pixel_fraction,
                        quality_flags=cached_flags,
                    )
                )
                continue
            stats = self._compute_stats(item, bbox)
            if not stats:
                continue
            point = NdviPoint(
                date=bucket_date,
                mean=stats.mean,
                min=stats.min,
                max=stats.max,
                sample_count=stats.sample_count,
                cloud_fraction=normalize_cloud_fraction(item.cloud_cover),
                valid_pixel_fraction=stats.valid_pixel_fraction,
                quality_flags=stats.quality_flags,
            )
            stats_cache[item.id] = (point, stats.quality_flags)
            points.append(point)
        return points

    def get_latest(
        self,
        *,
        bbox: BBox,
        lookback_days: int,
        max_cloud: int | None = None,
    ) -> NdviPoint | None:
        cloud = max_cloud if max_cloud is not None else get_default_max_cloud()
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
            cloud_fraction=normalize_cloud_fraction(item.cloud_cover),
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

    def _compute_stats(
        self,
        item: StacItem,
        bbox: BBox,
    ) -> NdviStats | None:
        nir_candidates = build_asset_candidates(self.asset_nir)
        scl_candidates = build_asset_candidates(self.asset_scl)
        nir_href = resolve_asset_href_candidates(item, nir_candidates)
        scl_href = resolve_asset_href_candidates(item, scl_candidates)

        if self.index_type == "NDWI":
            green_candidates = build_asset_candidates(self.asset_green)
            green_href = resolve_asset_href_candidates(item, green_candidates)
            if not green_href or not nir_href:
                logger.warning(
                    "stac.item.missing_assets item_id=%s",
                    getattr(item, "id", "-"),
                )
                return None
            index_array = load_ndwi_array(
                green_href=green_href,
                nir_href=nir_href,
                bbox=bbox,
                size=DEFAULT_STATS_SAMPLE_SIZE,
                timeout_seconds=self.timeout_seconds,
                scl_href=scl_href,
                mask_water=self.mask_water,
            )
        elif self.index_type == "NDMI":
            swir1_candidates = build_asset_candidates(self.asset_swir1)
            swir1_href = resolve_asset_href_candidates(item, swir1_candidates)
            if not swir1_href or not nir_href:
                logger.warning(
                    "stac.item.missing_assets item_id=%s",
                    getattr(item, "id", "-"),
                )
                return None
            index_array = load_ndmi_array(
                nir_href=nir_href,
                swir1_href=swir1_href,
                bbox=bbox,
                size=DEFAULT_STATS_SAMPLE_SIZE,
                timeout_seconds=self.timeout_seconds,
                scl_href=scl_href,
                mask_water=self.mask_water,
            )
        else:
            red_candidates = build_asset_candidates(self.asset_red)
            red_href = resolve_asset_href_candidates(item, red_candidates)
            if not red_href or not nir_href:
                logger.warning(
                    "stac.item.missing_assets item_id=%s",
                    getattr(item, "id", "-"),
                )
                return None
            index_array = load_ndvi_array(
                red_href=red_href,
                nir_href=nir_href,
                bbox=bbox,
                size=DEFAULT_STATS_SAMPLE_SIZE,
                timeout_seconds=self.timeout_seconds,
                scl_href=scl_href,
                mask_water=self.mask_water,
            )

        stats = compute_ndvi_stats(index_array)
        if stats is None:
            return None

        if scl_href is not None:
            valid_pixel_fraction = stats.valid_pixel_fraction
            quality_flags = stats.quality_flags
        else:
            valid_pixel_fraction = None
            quality_flags = None

        return NdviStats(
            mean=stats.mean,
            min=stats.min,
            max=stats.max,
            sample_count=stats.sample_count,
            valid_pixel_fraction=valid_pixel_fraction,
            quality_flags=quality_flags,
        )
