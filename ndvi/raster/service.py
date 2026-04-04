"""Raster rendering service layer.

This module orchestrates raster PNG generation for NDVI data.
It builds a RasterRequest, dispatches to the appropriate engine,
and returns the rendered PNG bytes with a content hash.

Auth: Handled by the view layer before calling this service.
Response: Binary PNG bytes (not wrapped in envelope).
"""

from __future__ import annotations

import hashlib
from datetime import date

from farms.models import Farm
from ndvi.engines.base import BBox

from .base import ColormapNormalization, RasterRequest
from .registry import get_engine, resolve_raster_engine_name


def _hash_png(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def render_ndvi_png(
    *,
    farm: Farm,
    bbox: BBox,
    day: date,
    size: int,
    max_cloud: int,
    engine_name: str | None = None,
    job_id: int | None = None,
    colormap_normalization: ColormapNormalization = (
        ColormapNormalization.HISTOGRAM
    ),
) -> tuple[bytes, str]:
    """Render a raster PNG and return content + hash.

    Args:
        farm: Farm instance for the raster.
        bbox: Bounding box for the raster.
        day: Date for the raster.
        size: Raster dimensions (width/height).
        max_cloud: Maximum cloud cover percentage.
        engine_name: Rendering engine ('stac' or 'sentinelhub').
        job_id: Optional job ID for tracking.
        colormap_normalization: Normalization strategy for the colormap.

    Returns:
        Tuple of (PNG bytes, SHA256 hash).
    """
    resolved_engine = resolve_raster_engine_name(engine_name)
    request = RasterRequest(
        bbox=bbox,
        date=day,
        size=size,
        max_cloud=max_cloud,
        engine=resolved_engine,
        job_id=job_id,
        farm_id=farm.id,
        colormap_normalization=colormap_normalization,
    )
    engine = get_engine(resolved_engine)
    content = engine.render_png(request)
    return content, _hash_png(content)
