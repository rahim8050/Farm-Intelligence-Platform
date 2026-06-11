"""MODIS NDVI engine adapter.

Provides NDVI from MODIS (Terra/Aqua) as a fallback for temporal
continuity when Sentinel-2 and Landsat are both unavailable.

Uses the MODIS MOD13Q1/MYD13Q1 NDVI product which contains
pre-computed NDVI (not computed from red/nir bands). Available
via STAC (default: Microsoft Planetary Computer).
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from datetime import date, timedelta
from typing import Any, Final

import httpx
import numpy as np
from django.conf import settings

from ndvi.engines.base import BBox, NDVIEngine, NdviPoint
from ndvi.metrics import (
    ndvi_upstream_latency_seconds,
    ndvi_upstream_requests_total,
)
from ndvi.stac_client import (
    DEFAULT_STATS_SAMPLE_SIZE,
    StacClient,
    build_asset_candidates,
    select_best_item,
)


class UnsupportedIndexError(ValueError):
    """Raised when the engine does not support the requested index type."""


logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0
DEFAULT_DATE_WINDOW_DAYS: Final[int] = 5
DEFAULT_MAX_CLOUD: Final[int] = 30
DEFAULT_NDVI_BAND: Final[str] = "NDVI"
DEFAULT_QA_BAND: Final[str] = "DetailedQA"
MODIS_NDVI_SCALE: Final[float] = 0.0001


def _str_setting(name: str, default: str) -> str:
    return str(getattr(settings, f"NDVI_MODIS_{name}", default))


def _int_setting(name: str, default: int) -> int:
    return int(getattr(settings, f"NDVI_MODIS_{name}", default))


def _float_setting(name: str, default: float) -> float:
    return float(getattr(settings, f"NDVI_MODIS_{name}", default))


def _is_remote_href(href: str) -> bool:
    return bool(href.startswith(("http://", "https://")))


def _load_single_band(
    href: str,
    *,
    bbox: BBox,
    size: int,
    timeout_seconds: float,
    scale_factor: float | None = None,
    qa_href: str | None = None,
) -> np.ndarray:
    """Load a single-band COG array, optionally quality-masked."""
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.warp import transform_bounds

    gdal_env: dict[str, object] = {
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "GDAL_HTTP_TIMEOUT": int(timeout_seconds),
        "GDAL_HTTP_CONNECTTIMEOUT": int(timeout_seconds),
        "GDAL_HTTP_MAX_RETRY": 5,
        "GDAL_HTTP_RETRY_DELAY": 2,
        "GDAL_NUM_THREADS": "ALL_CPUS",
        "GDAL_CACHEMAX": 256,
    }

    with tempfile.TemporaryDirectory(prefix="ndvi_modis_") as tmpdir:
        local_path = _download_asset(href, tmpdir, timeout_seconds)
        qa_local: str | None = None
        if qa_href:
            try:
                qa_local = _download_asset(qa_href, tmpdir, timeout_seconds)
            except Exception:
                logger.warning("modis.qa.download_failed href=%s", qa_href)

        with rasterio.Env(**gdal_env), rasterio.open(local_path) as src:
            if src.crs is None:
                return np.array([])
            bounds = transform_bounds(
                "EPSG:4326",
                src.crs,
                float(bbox.west),
                float(bbox.south),
                float(bbox.east),
                float(bbox.north),
                densify_pts=21,
            )
            out_shape = (
                int(src.height * size / max(src.width, src.height)),
                size,
            )
            window = src.window(*bounds)
            data = src.read(
                1,
                window=window,
                out_shape=out_shape,
                resampling=Resampling.bilinear,
            ).astype(np.float64)

        if scale_factor is not None:
            data = data * scale_factor

        if qa_local:
            try:
                with (
                    rasterio.Env(**gdal_env),
                    rasterio.open(qa_local) as qa_src,
                ):
                    qa = qa_src.read(1, window=window, out_shape=out_shape)
                    mask = (qa & 0b11) != 0
                    data[mask] = np.nan
            except Exception:
                logger.warning("modis.qa.apply_failed path=%s", qa_local)

        data[np.isinf(data)] = np.nan
        data[(data < -1.0) | (data > 1.0)] = np.nan
        return data


def _download_asset(href: str, tmpdir: str, timeout_seconds: float) -> str:
    """Download a remote COG asset to a temp directory."""
    if not _is_remote_href(href):
        return href
    filename = os.path.basename(href.split("?")[0])
    local_path = os.path.join(tmpdir, filename)
    client = httpx.Client(timeout=timeout_seconds, follow_redirects=True)
    started = time.monotonic()
    resp = client.get(href)
    ndvi_upstream_latency_seconds.labels(engine="modis_raster").observe(
        time.monotonic() - started
    )
    resp.raise_for_status()
    ndvi_upstream_requests_total.labels(
        engine="modis_raster", outcome="success"
    ).inc()
    with open(local_path, "wb") as f:
        f.write(resp.content)
    return local_path


def _compute_band_stats(
    array: np.ndarray,
) -> dict[str, float | int | None]:
    if array.size == 0 or bool(np.isnan(array).all()):
        return {"mean": None, "min": None, "max": None, "sample_count": 0}
    return {
        "mean": float(np.nanmean(array)),
        "min": None if np.isnan(np.nanmin(array)) else float(np.nanmin(array)),
        "max": None if np.isnan(np.nanmax(array)) else float(np.nanmax(array)),
        "sample_count": int(np.count_nonzero(~np.isnan(array))),
    }


class ModisEngine(NDVIEngine):
    """NDVI engine backed by MODIS MOD13Q1 via STAC.

    Uses the pre-computed NDVI band from the MODIS Vegetation Index
    product. Defaults to Microsoft Planetary Computer STAC API.
    """

    engine_name: Final[str] = "modis"

    def __init__(
        self,
        *,
        client: StacClient | None = None,
        timeout_seconds: float | None = None,
        date_window_days: int | None = None,
        index_type: str = "NDVI",
        ndvi_band: str | None = None,
        qa_band: str | None = None,
    ) -> None:
        if index_type != "NDVI":
            raise UnsupportedIndexError(
                f"MODIS engine only supports NDVI, not {index_type}. "
                "MODIS provides pre-computed NDVI (MOD13Q1) and does not "
                "have the Green band needed for NDWI computation."
            )
        self.index_type = index_type
        self.timeout_seconds = timeout_seconds or _float_setting(
            "TIMEOUT_SECS", DEFAULT_TIMEOUT_SECONDS
        )
        self.date_window_days = date_window_days or _int_setting(
            "DATE_WINDOW_DAYS", DEFAULT_DATE_WINDOW_DAYS
        )
        self.ndvi_band = ndvi_band or _str_setting(
            "NDVI_BAND", DEFAULT_NDVI_BAND
        )
        self.qa_band = qa_band or _str_setting("QA_BAND", DEFAULT_QA_BAND)
        _stac_url = _str_setting(
            "STAC_API_URL",
            "https://planetarycomputer.microsoft.com/api/stac/v1/",
        )
        _collection = _str_setting("STAC_COLLECTION", "modis-13q1-061")
        self.client = client or StacClient(
            base_url=_stac_url,
            collection=_collection,
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
        for bucket_date in self._iter_buckets(start, end, step_days):
            item = select_best_item(
                items,
                target_date=bucket_date,
                window_days=self.date_window_days,
            )
            if not item:
                continue
            point = self._process_item(item, bbox, bucket_date)
            if point is not None:
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
        return self._process_item(item, bbox, item.date)

    def _iter_buckets(
        self, start: date, end: date, step_days: int
    ) -> list[date]:
        buckets: list[date] = []
        cursor = start
        while cursor <= end:
            buckets.append(cursor)
            cursor = cursor + timedelta(days=step_days)
        return buckets

    def _process_item(
        self, item: Any, bbox: BBox, bucket_date: date
    ) -> NdviPoint | None:
        ndvi_assets = build_asset_candidates(self.ndvi_band)
        qa_assets = build_asset_candidates(self.qa_band)
        from ndvi.stac_client import resolve_asset_href_candidates

        ndvi_href = resolve_asset_href_candidates(item, ndvi_assets)
        if not ndvi_href:
            logger.warning(
                "modis.item.missing_ndvi item_id=%s",
                getattr(item, "id", "-"),
            )
            return None
        qa_href = resolve_asset_href_candidates(item, qa_assets)

        array = _load_single_band(
            ndvi_href,
            bbox=bbox,
            size=DEFAULT_STATS_SAMPLE_SIZE,
            timeout_seconds=self.timeout_seconds,
            scale_factor=MODIS_NDVI_SCALE,
            qa_href=qa_href,
        )
        if array.size == 0:
            return None

        stats = _compute_band_stats(array)
        if stats["mean"] is None:
            return None

        sample_count = stats["sample_count"]
        return NdviPoint(
            date=bucket_date,
            mean=stats["mean"],
            min=stats["min"],
            max=stats["max"],
            sample_count=int(sample_count)
            if sample_count is not None
            else None,
            quality_flags={"modis": True, "pre_computed_ndvi": True},
        )
