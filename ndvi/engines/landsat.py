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
        points: list[NdviPoint] = []
        items = self.client.search(
            bbox=bbox,
            start=start - window,
            end=end + window,
            max_cloud=cloud,
        )

        for bucket_date in self._iter_buckets(start, end, step_days):
            item = select_best_item(
                items,
                target_date=bucket_date,
                window_days=self.date_window_days,
            )
            if not item:
                continue
            stats = self._compute_stats(item, bbox)
            if not stats:
                continue
            points.append(
                NdviPoint(
                    date=bucket_date,
                    mean=stats.mean,
                    min=stats.min,
                    max=stats.max,
                    sample_count=stats.sample_count,
                    cloud_fraction=getattr(item, "cloud_cover", None),
                    valid_pixel_fraction=stats.valid_pixel_fraction,
                    quality_flags=stats.quality_flags,
                )
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
        nir_assets = build_asset_candidates(self.asset_nir)
        nir_href = resolve_asset_href_candidates(item, nir_assets)

        if self.index_type == "NDWI":
            green_assets = build_asset_candidates(self.asset_green)
            green_href = resolve_asset_href_candidates(item, green_assets)
            if not green_href or not nir_href:
                logger.warning(
                    "landsat.item.missing_assets item_id=%s",
                    getattr(item, "id", "-"),
                )
                return None
            index_array = load_ndwi_array(
                green_href=green_href,
                nir_href=nir_href,
                bbox=bbox,
                size=DEFAULT_STATS_SAMPLE_SIZE,
                timeout_seconds=self.timeout_seconds,
            )
        else:
            red_assets = build_asset_candidates(self.asset_red)
            red_href = resolve_asset_href_candidates(item, red_assets)
            if not red_href or not nir_href:
                logger.warning(
                    "landsat.item.missing_assets item_id=%s",
                    getattr(item, "id", "-"),
                )
                return None
            index_array = load_ndvi_array(
                red_href=red_href,
                nir_href=nir_href,
                bbox=bbox,
                size=DEFAULT_STATS_SAMPLE_SIZE,
                timeout_seconds=self.timeout_seconds,
            )
        return compute_ndvi_stats(index_array)
