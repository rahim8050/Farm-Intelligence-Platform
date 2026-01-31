from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from ndvi.engines.base import BBox


@dataclass(frozen=True)
class RasterRequest:
    """Normalized raster request parameters."""

    bbox: BBox
    date: date
    size: int
    max_cloud: int
    engine: str
    job_id: int | None = None
    farm_id: int | None = None


class NdviRasterEngine(Protocol):
    """Interface for rendering NDVI rasters as PNG images."""

    def render_png(self, request: RasterRequest) -> bytes:
        """Render a PNG heatmap for the given request."""
