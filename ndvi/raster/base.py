from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Protocol

from ndvi.engines.base import BBox


class ColormapNormalization(StrEnum):
    """Colormap normalization strategy for NDVI raster visualization."""

    HISTOGRAM = "histogram"
    """Per-image min-max stretching. Shows maximum detail within each image."""

    FIXED = "fixed"
    """Fixed NDVI range mapping. Consistent colors across different images."""


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
    colormap_normalization: ColormapNormalization = (
        ColormapNormalization.HISTOGRAM
    )


class NdviRasterEngine(Protocol):
    """Interface for rendering NDVI rasters as PNG images."""

    def render_png(self, request: RasterRequest) -> bytes:
        """Render a PNG heatmap for the given request."""
