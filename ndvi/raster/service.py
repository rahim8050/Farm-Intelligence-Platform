from __future__ import annotations

import hashlib
from datetime import date

from farms.models import Farm
from ndvi.engines.base import BBox

from .base import RasterRequest
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
) -> tuple[bytes, str]:
    """Render a raster PNG and return content + hash."""

    resolved_engine = resolve_raster_engine_name(engine_name)
    request = RasterRequest(
        bbox=bbox,
        date=day,
        size=size,
        max_cloud=max_cloud,
        engine=resolved_engine,
        job_id=job_id,
        farm_id=farm.id,
    )
    engine = get_engine(resolved_engine)
    content = engine.render_png(request)
    return content, _hash_png(content)
