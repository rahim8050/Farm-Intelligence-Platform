"""STAC-based data provider for spectral index computation.

Provides search, band loading, and latest-item retrieval using
a STAC API client.  Uses ``BAND_REGISTRY`` to resolve abstract
band names to sensor-specific asset keys.
"""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import date, timedelta

import httpx
import numpy as np

from ndvi.engines.base import BBox
from ndvi.logging import StructuredLogger, Timer
from ndvi.stac_client import (
    DEFAULT_STATS_SAMPLE_SIZE,
    StacClient,
    StacItem,
    build_asset_candidates,
    resolve_asset_href_candidates,
    select_best_item,
)

logger = logging.getLogger(__name__)
slog = StructuredLogger(__name__)


def _is_remote_href(href: str) -> bool:
    return bool(href.startswith(("http://", "https://")))


def _download_asset(href: str, tmpdir: str, timeout_seconds: float) -> str:
    """Download a remote COG asset to a temp directory."""
    if not _is_remote_href(href):
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


def _load_single_band(
    href: str,
    bbox: BBox,
    timeout_seconds: float = 30.0,
) -> np.ndarray:
    """Load a single-band COG as a numpy array.

    Downloads remote COGs to a temp directory first to avoid streaming
    decode issues, then reads with rasterio.
    """
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

    with tempfile.TemporaryDirectory(prefix="ndmi_stac_") as tmpdir:
        local_path = _download_asset(href, tmpdir, timeout_seconds)
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
                int(
                    src.height
                    * DEFAULT_STATS_SAMPLE_SIZE
                    / max(src.width, src.height)
                ),
                DEFAULT_STATS_SAMPLE_SIZE,
            )
            window = src.window(*bounds)
            data = src.read(
                1,
                window=window,
                out_shape=out_shape,
                resampling=Resampling.bilinear,
            ).astype(np.float32)
            return data


class StacDataProvider:
    """Data provider backed by a STAC API.

    Args:
        client: ``StacClient`` instance (or ``None`` to create a default).
        sensor_key: e.g. ``"sentinel2_l2a"`` — used with ``BAND_REGISTRY``.
        timeout_seconds: HTTP timeout for band downloads.
    """

    def __init__(
        self,
        *,
        client: StacClient | None = None,
        sensor_key: str = "sentinel2_l2a",
        timeout_seconds: float = 30.0,
    ) -> None:
        self.sensor_key = sensor_key
        self.timeout_seconds = timeout_seconds
        self.client = client or StacClient(timeout_seconds=timeout_seconds)

    def search(
        self,
        bbox: BBox,
        start: date,
        end: date,
        max_cloud: int,
    ) -> list[StacItem]:
        """Search the STAC API for items matching the query."""
        timer = Timer()
        items = self.client.search(
            bbox=bbox,
            start=start,
            end=end,
            max_cloud=max_cloud,
        )
        slog.info(
            "provider.search",
            f"STAC search done sensor={self.sensor_key} items={len(items)}",
            provider=self.sensor_key,
            duration_ms=timer.elapsed_ms(),
            item_count=len(items),
            bbox=str(bbox),
            start=str(start),
            end=str(end),
        )
        return items

    def load_band(
        self,
        item: StacItem,
        band_asset_key: str,
        bbox: BBox,
    ) -> np.ndarray:
        """Load a single band array from a STAC item asset."""
        timer = Timer()
        candidates = build_asset_candidates(band_asset_key)
        href = resolve_asset_href_candidates(item, candidates)
        if not href:
            logger.warning(
                "stac.band_resolve_failed item_id=%s asset=%s",
                item.id,
                band_asset_key,
            )
            return np.array([])
        result = _load_single_band(
            href,
            bbox,
            timeout_seconds=self.timeout_seconds,
        )
        slog.info(
            "provider.load_band",
            f"Band loaded sensor={self.sensor_key} "
            f"asset={band_asset_key} size={result.size}",
            provider=self.sensor_key,
            duration_ms=timer.elapsed_ms(),
            band_asset_key=band_asset_key,
            band_size=result.size,
        )
        return result

    def get_latest(
        self,
        bbox: BBox,
        lookback_days: int,
        max_cloud: int,
    ) -> StacItem | None:
        """Return the most recent item within the lookback window."""
        timer = Timer()
        today = date.today()
        start = today - timedelta(days=lookback_days)
        items = self.client.search(
            bbox=bbox,
            start=start,
            end=today,
            max_cloud=max_cloud,
        )
        result = select_best_item(
            items,
            target_date=today,
            window_days=lookback_days,
        )
        slog.info(
            "provider.get_latest",
            f"Latest item sensor={self.sensor_key} found={result is not None}",
            provider=self.sensor_key,
            duration_ms=timer.elapsed_ms(),
            has_item=result is not None,
        )
        return result
