"""STAC-based NDVI engine using COG assets."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Final

import numpy as np
from django.conf import settings

from ndvi.engines.base import BBox, NDVIEngine, NdviPoint
from ndvi.stac_client import (
    NdviStats,
    StacClient,
    StacItem,
    build_asset_candidates,
    compute_ndvi_stats,
    normalize_cloud_fraction,
    resolve_asset_href_candidates,
    select_best_item,
)
from science.formulas.registry import FORMULA_REGISTRY, compute_index

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

# Map abstract band names to StacEngine asset attributes
_BAND_TO_ASSET_ATTR: Final[dict[str, str]] = {
    "red": "asset_red",
    "green": "asset_green",
    "nir": "asset_nir",
    "swir1": "asset_swir1",
}


def _load_stac_index_array(
    engine: StacEngine, item: StacItem, bbox: BBox
) -> np.ndarray:
    """Load and compute any spectral index using FORMULA_REGISTRY.

    Resolves abstract band names from the formula registry to engine-specific
    asset attributes, loads each band, computes the index via
    ``compute_index()``, and applies SCL masking if available.
    """
    formula = FORMULA_REGISTRY.get(engine.index_type)
    if formula is None:
        logger.warning("stac.unknown_index index=%s", engine.index_type)
        return np.array([])

    # Resolve hrefs for required bands
    band_hrefs: dict[str, str] = {}
    for band_name in formula["bands"]:
        attr_name = _BAND_TO_ASSET_ATTR.get(band_name)
        if attr_name is None:
            logger.warning(
                "stac.band_not_mapped band=%s index=%s",
                band_name,
                engine.index_type,
            )
            return np.array([])
        asset_key = getattr(engine, attr_name)
        href = resolve_asset_href_candidates(
            item, build_asset_candidates(asset_key)
        )
        if not href:
            logger.warning(
                "stac.item.missing_band band=%s item_id=%s",
                band_name,
                getattr(item, "id", "-"),
            )
            return np.array([])
        band_hrefs[band_name] = href

    # Resolve SCL href for masking
    scl_href = resolve_asset_href_candidates(
        item, build_asset_candidates(engine.asset_scl)
    )

    # Load bands individually
    band_arrays: dict[str, np.ndarray] = {}
    for band_name, href in band_hrefs.items():
        arr = _load_single_stac_band(
            href, bbox, timeout_seconds=engine.timeout_seconds
        )
        if arr.size == 0:
            return np.array([])
        band_arrays[band_name] = arr

    # Compute the spectral index using the registered formula
    index_array = compute_index(engine.index_type, **band_arrays)

    # Apply SCL mask if available
    if scl_href:
        scl_arr = _load_single_stac_band(
            scl_href, bbox, timeout_seconds=engine.timeout_seconds
        )
        if scl_arr.size > 0:
            from ndvi.stac_client import apply_scl_mask

            index_array, _, _ = apply_scl_mask(
                index_array, scl_arr, mask_water=engine.mask_water
            )

    if index_array.size == 0:
        return np.array([])
    return index_array.astype(np.float32)


def _load_single_stac_band(
    href: str,
    bbox: BBox,
    timeout_seconds: float = 30.0,
    size: int = 256,
) -> np.ndarray:
    """Load a single COG band as a numpy array.

    Downloads remote COGs to a temp directory, then reads with rasterio.
    Mirrors the per-band loading pattern in ``providers/stac.py``.
    """
    import os
    import tempfile

    import httpx
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

    def _download(href: str, tmpdir: str) -> str:
        if not href.startswith(("http://", "https://")):
            return href
        filename = os.path.basename(href.split("?")[0])
        local_path = os.path.join(tmpdir, filename)
        client = httpx.Client(timeout=timeout_seconds, follow_redirects=True)
        try:
            resp = client.get(href)
            resp.raise_for_status()
            with open(local_path, "wb") as f:
                f.write(resp.content)
        finally:
            client.close()
        return local_path

    with tempfile.TemporaryDirectory(prefix="ndvi_stac_") as tmpdir:
        local_path = _download(href, tmpdir)
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
            ).astype(np.float32)
            return data


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
        index_array = _load_stac_index_array(self, item, bbox)
        if index_array.size == 0:
            return None

        stats = compute_ndvi_stats(index_array)
        if stats is None:
            return None

        return NdviStats(
            mean=stats.mean,
            min=stats.min,
            max=stats.max,
            sample_count=stats.sample_count,
            valid_pixel_fraction=stats.valid_pixel_fraction,
            quality_flags=stats.quality_flags,
        )
