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
    compute_ndvi_stats,
    load_ndvi_array,
    normalize_cloud_fraction,
    resolve_asset_href,
    select_best_item,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0
DEFAULT_DATE_WINDOW_DAYS: Final[int] = 3
DEFAULT_MAX_CLOUD: Final[int] = 30
DEFAULT_ASSET_RED: Final[str] = "B04"
DEFAULT_ASSET_NIR: Final[str] = "B08"


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


class StacEngine(NDVIEngine):
    """Fetch NDVI metrics from a STAC API."""

    engine_name: Final[str] = "stac"

    def __init__(
        self,
        *,
        client: StacClient | None = None,
        timeout_seconds: float | None = None,
        date_window_days: int | None = None,
        asset_red: str | None = None,
        asset_nir: str | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds or get_default_timeout_seconds()
        self.date_window_days = (
            date_window_days or get_default_date_window_days()
        )
        self.asset_red = asset_red or get_default_asset_red()
        self.asset_nir = asset_nir or get_default_asset_nir()
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
        stats_cache: dict[str, NdviPoint] = {}

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
                points.append(
                    NdviPoint(
                        date=bucket_date,
                        mean=cached.mean,
                        min=cached.min,
                        max=cached.max,
                        sample_count=cached.sample_count,
                        cloud_fraction=cached.cloud_fraction,
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
            )
            stats_cache[item.id] = point
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
        red_href = resolve_asset_href(item, self.asset_red)
        nir_href = resolve_asset_href(item, self.asset_nir)
        if not red_href or not nir_href:
            logger.warning(
                "stac.item.missing_assets item_id=%s", getattr(item, "id", "-")
            )
            return None
        ndvi = load_ndvi_array(
            red_href=red_href,
            nir_href=nir_href,
            bbox=bbox,
            size=DEFAULT_STATS_SAMPLE_SIZE,
            timeout_seconds=self.timeout_seconds,
        )
        stats = compute_ndvi_stats(ndvi)
        return stats
