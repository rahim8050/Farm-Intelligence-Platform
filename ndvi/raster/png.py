"""Deterministic helpers for converting NDVI rasters into PNG previews.

The NDVI raster endpoint already bounds raster dimensions at the request layer.
These helpers validate array shape and dtype, normalize values to the expected
range, apply the canonical red-yellow-green colormap, and encode a binary PNG.
"""

from __future__ import annotations

import io
import os
import tempfile
from pathlib import Path
from typing import Any, Final

import numpy as np
from PIL import Image

NDVI_MIN: Final[float] = -1.0
NDVI_MAX: Final[float] = 1.0
NDVI_COLORMAP_NAME: Final[str] = "RdYlGn"
PNG_SIGNATURE: Final[bytes] = b"\x89PNG\r\n\x1a\n"
MPLCONFIGDIR_ENV: Final[str] = "MPLCONFIGDIR"
MPLCONFIGDIR_PATH: Final[Path] = (
    Path(tempfile.gettempdir()) / "weather-apis-matplotlib"
)
RDYL_GN_CONTROL_POINTS: Final[np.ndarray] = np.array(
    [
        [165, 0, 38],
        [215, 48, 39],
        [244, 109, 67],
        [253, 174, 97],
        [254, 224, 139],
        [255, 255, 191],
        [217, 239, 139],
        [166, 217, 106],
        [102, 189, 99],
        [26, 152, 80],
        [0, 104, 55],
    ],
    dtype=np.float32,
)


def _validated_ndvi(ndvi: np.ndarray) -> np.ndarray:
    """Return a writable 2D float32 NDVI array after validating the input."""

    if ndvi.ndim != 2:
        raise ValueError("NDVI array must be two-dimensional.")
    if ndvi.size == 0:
        raise ValueError("NDVI array must not be empty.")
    if ndvi.dtype not in (np.float32, np.float64):
        raise TypeError("NDVI array must use float32 or float64 dtype.")
    return np.array(ndvi, dtype=np.float32, copy=True)


def _fallback_rdylgn_bytes(normalized: np.ndarray) -> np.ndarray:
    """Approximate matplotlib's RdYlGn colormap using fixed control points."""

    positions = np.linspace(
        0.0,
        1.0,
        num=RDYL_GN_CONTROL_POINTS.shape[0],
        dtype=np.float32,
    )
    red = np.interp(
        normalized,
        positions,
        RDYL_GN_CONTROL_POINTS[:, 0],
    )
    green = np.interp(
        normalized,
        positions,
        RDYL_GN_CONTROL_POINTS[:, 1],
    )
    blue = np.interp(
        normalized,
        positions,
        RDYL_GN_CONTROL_POINTS[:, 2],
    )
    return np.stack([red, green, blue], axis=-1).round().astype(np.uint8)


def _load_matplotlib_colormaps() -> Any:
    """Load matplotlib colormaps using a writable cache directory."""

    if MPLCONFIGDIR_ENV not in os.environ:
        MPLCONFIGDIR_PATH.mkdir(parents=True, exist_ok=True)
        os.environ[MPLCONFIGDIR_ENV] = str(MPLCONFIGDIR_PATH)

    from matplotlib import colormaps

    return colormaps


def ndvi_to_rgb(ndvi: np.ndarray) -> np.ndarray:
    """Map a 2D NDVI array into an RGB uint8 image using RdYlGn."""

    ndvi_float = _validated_ndvi(ndvi)
    np.nan_to_num(
        ndvi_float,
        copy=False,
        nan=0.0,
        posinf=NDVI_MAX,
        neginf=NDVI_MIN,
    )
    np.clip(ndvi_float, NDVI_MIN, NDVI_MAX, out=ndvi_float)
    normalized = (ndvi_float + 1.0) / 2.0

    if np.isclose(np.nanmax(ndvi_float), np.nanmin(ndvi_float), atol=1e-6):
        raise ValueError("NDVI has no variation")

    try:
        colormaps = _load_matplotlib_colormaps()
    except ImportError:
        rgb = _fallback_rdylgn_bytes(normalized)
    else:
        colored = colormaps[NDVI_COLORMAP_NAME](normalized, bytes=True)
        rgb = np.ascontiguousarray(colored[:, :, :3])
    if rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[2] != 3:
        raise TypeError("Colormap output must be an RGB uint8 image.")
    return rgb


def rgb_to_png_bytes(rgb: np.ndarray) -> bytes:
    """Encode an RGB uint8 array as binary PNG bytes."""

    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError("RGB array must have shape (H, W, 3).")
    if rgb.size == 0:
        raise ValueError("RGB array must not be empty.")
    if rgb.dtype != np.uint8:
        raise TypeError("RGB array must use uint8 dtype.")

    image = Image.fromarray(np.ascontiguousarray(rgb), mode="RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", compress_level=9, optimize=False)
    return buffer.getvalue()


def ndvi_to_png_bytes(ndvi: np.ndarray) -> bytes:
    """Convert a validated NDVI array into deterministic PNG bytes."""

    return rgb_to_png_bytes(ndvi_to_rgb(ndvi))
