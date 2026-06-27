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
    NdviStats,
    StacClient,
    build_asset_candidates,
    compute_ndvi_stats,
    resolve_asset_href_candidates,
    select_best_item,
)
from science.formulas.registry import FORMULA_REGISTRY, compute_index

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

# Map abstract band names (from FORMULA_REGISTRY) to GeeEngine asset attributes
_BAND_TO_ASSET_ATTR: Final[dict[str, str]] = {
    "red": "asset_red",
    "green": "asset_green",
    "nir": "asset_nir",
}


def _load_gee_index_array(
    engine: GeeEngine, item: Any, bbox: BBox
) -> np.ndarray:
    """Load and compute any spectral index using FORMULA_REGISTRY.

    Resolves abstract band names from the formula registry to engine-specific
    asset attributes, loads each band, computes the index via
    ``compute_index()``, and applies SCL masking if available.
    """
    formula = FORMULA_REGISTRY.get(engine.index_type)
    if formula is None:
        logger.warning("gee.unknown_index index=%s", engine.index_type)
        return np.array([])

    # Resolve hrefs for required bands
    band_hrefs: dict[str, str] = {}
    for band_name in formula["bands"]:
        attr_name = _BAND_TO_ASSET_ATTR.get(band_name)
        if attr_name is None:
            logger.warning(
                "gee.band_not_mapped band=%s index=%s",
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
                "gee.item.missing_band band=%s item_id=%s",
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
        arr = _load_single_gee_band(
            href, bbox, timeout_seconds=engine.timeout_seconds
        )
        if arr.size == 0:
            return np.array([])
        band_arrays[band_name] = arr

    # Compute the spectral index using the registered formula
    index_array = compute_index(engine.index_type, **band_arrays)

    # Apply SCL mask if available
    if scl_href:
        scl_arr = _load_single_gee_band(
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


def _load_single_gee_band(
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

    with tempfile.TemporaryDirectory(prefix="ndvi_gee_") as tmpdir:
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
        index_array = _load_gee_index_array(self, item, bbox)
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
