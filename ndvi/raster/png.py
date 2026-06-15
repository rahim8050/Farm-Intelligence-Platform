"""Deterministic helpers for converting spectral index rasters into PNG
previews.

NDVI uses the RdYlGn (Red-Yellow-Green) colormap — green indicates high
vegetation. NDWI uses the BrBG (Brown-Blue-Green) diverging colormap —
blue/cyan indicates high water content (positive NDWI), brown/tan indicates
dry/low water (negative NDWI).

Colormap Normalization Modes
-----------------------------
- histogram: Per-image min-max stretching. Shows maximum detail within each
  image by utilizing the full colormap spectrum for the actual index range.
- fixed: Fixed NDVI range mapping [-1.0, 1.0] → [0.0, 1.0]. Provides consistent
  colors across different images (e.g., 0.5 always maps to yellow for NDVI).
"""

from __future__ import annotations

import io
import os
import tempfile
from pathlib import Path
from typing import Any, Final

import numpy as np
from PIL import Image

from ndvi.raster.base import ColormapNormalization

NDVI_MIN: Final[float] = -1.0
NDVI_MAX: Final[float] = 1.0
NDVI_COLORMAP_NAME: Final[str] = "RdYlGn"
PNG_SIGNATURE: Final[bytes] = b"\x89PNG\r\n\x1a\n"
MPLCONFIGDIR_ENV: Final[str] = "MPLCONFIGDIR"
MPLCONFIGDIR_PATH: Final[Path] = (
    Path(tempfile.gettempdir()) / "farm-intelligence-platform-matplotlib"
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

NDWI_COLORMAP_NAME: Final[str] = "BrBG"
BRBG_CONTROL_POINTS: Final[np.ndarray] = np.array(
    [
        [84, 48, 5],
        [140, 81, 10],
        [191, 129, 45],
        [223, 194, 125],
        [246, 232, 195],
        [245, 245, 245],
        [199, 234, 229],
        [128, 205, 193],
        [53, 151, 143],
        [1, 102, 94],
        [0, 60, 48],
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


def _fallback_brbg_bytes(normalized: np.ndarray) -> np.ndarray:
    """Approximate matplotlib's BrBG colormap using fixed control points."""

    positions = np.linspace(
        0.0,
        1.0,
        num=BRBG_CONTROL_POINTS.shape[0],
        dtype=np.float32,
    )
    red = np.interp(
        normalized,
        positions,
        BRBG_CONTROL_POINTS[:, 0],
    )
    green = np.interp(
        normalized,
        positions,
        BRBG_CONTROL_POINTS[:, 1],
    )
    blue = np.interp(
        normalized,
        positions,
        BRBG_CONTROL_POINTS[:, 2],
    )
    return np.stack([red, green, blue], axis=-1).round().astype(np.uint8)


def _load_matplotlib_colormaps() -> Any:
    """Load matplotlib colormaps using a writable cache directory."""

    if MPLCONFIGDIR_ENV not in os.environ:
        MPLCONFIGDIR_PATH.mkdir(parents=True, exist_ok=True)
        os.environ[MPLCONFIGDIR_ENV] = str(MPLCONFIGDIR_PATH)

    from matplotlib import colormaps

    return colormaps


def _normalize_ndvi(
    ndvi_float: np.ndarray,
    mode: ColormapNormalization,
) -> np.ndarray:
    """Normalize NDVI values to [0, 1] range for colormap application.

    Args:
        ndvi_float: 2D NDVI array (already validated and clipped to [-1, 1]).
        mode: Normalization strategy.

    Returns:
        Normalized array in [0, 1] range.
    """
    if mode == ColormapNormalization.HISTOGRAM:
        ndvi_min = float(np.nanmin(ndvi_float))
        ndvi_max = float(np.nanmax(ndvi_float))
        if np.isclose(ndvi_max, ndvi_min, atol=1e-6):
            raise ValueError("NDVI has no variation")
        return (ndvi_float - ndvi_min) / (ndvi_max - ndvi_min)
    else:
        # Fixed mapping: [-1, 1] → [0, 1]
        return (ndvi_float + 1.0) / 2.0


def ndvi_to_rgb(
    ndvi: np.ndarray,
    colormap_normalization: ColormapNormalization = (
        ColormapNormalization.HISTOGRAM
    ),
) -> np.ndarray:
    """Map a 2D NDVI array into an RGB uint8 image using RdYlGn.

    Args:
        ndvi: 2D NDVI array with float32/float64 dtype.
        colormap_normalization: Strategy for mapping NDVI to colormap.
            HISTOGRAM stretches the actual NDVI range to use full colormap.
            FIXED uses a constant [-1, 1] → [0, 1] mapping.

    Returns:
        RGB uint8 array with shape (H, W, 3).
    """
    ndvi_float = _validated_ndvi(ndvi)
    np.nan_to_num(
        ndvi_float,
        copy=False,
        nan=0.0,
        posinf=NDVI_MAX,
        neginf=NDVI_MIN,
    )
    np.clip(ndvi_float, NDVI_MIN, NDVI_MAX, out=ndvi_float)
    normalized = _normalize_ndvi(ndvi_float, colormap_normalization)

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


def ndvi_to_png_bytes(
    ndvi: np.ndarray,
    colormap_normalization: ColormapNormalization = (
        ColormapNormalization.HISTOGRAM
    ),
) -> bytes:
    """Convert a validated NDVI array into deterministic PNG bytes.

    Args:
        ndvi: 2D NDVI array with float32/float64 dtype.
        colormap_normalization: Strategy for mapping NDVI to colormap.

    Returns:
        Binary PNG bytes.
    """
    return rgb_to_png_bytes(
        ndvi_to_rgb(ndvi, colormap_normalization=colormap_normalization)
    )


def ndwi_to_rgb(
    ndwi: np.ndarray,
    colormap_normalization: ColormapNormalization = (
        ColormapNormalization.HISTOGRAM
    ),
) -> np.ndarray:
    """Map a 2D NDWI array into an RGB uint8 image using BrBG colormap.

    BrBG (Brown-Blue-Green) diverging colormap shows water (positive NDWI)
    as blue/cyan and dry/low water (negative NDWI) as brown/tan. This is the
    inverse visual mapping from the RdYlGn used for NDVI.

    Args:
        ndwi: 2D NDWI array with float32/float64 dtype.
        colormap_normalization: Strategy for mapping NDWI to colormap.

    Returns:
        RGB uint8 array with shape (H, W, 3).
    """
    ndwi_float = _validated_ndvi(ndwi)
    np.nan_to_num(
        ndwi_float,
        copy=False,
        nan=0.0,
        posinf=1.0,
        neginf=-1.0,
    )
    np.clip(ndwi_float, -1.0, 1.0, out=ndwi_float)
    normalized = _normalize_ndvi(ndwi_float, colormap_normalization)

    try:
        colormaps = _load_matplotlib_colormaps()
    except ImportError:
        rgb = _fallback_brbg_bytes(normalized)
    else:
        colored = colormaps[NDWI_COLORMAP_NAME](normalized, bytes=True)
        rgb = np.ascontiguousarray(colored[:, :, :3])
    if rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[2] != 3:
        raise TypeError("Colormap output must be an RGB uint8 image.")
    return rgb


def ndwi_to_png_bytes(
    ndwi: np.ndarray,
    colormap_normalization: ColormapNormalization = (
        ColormapNormalization.HISTOGRAM
    ),
) -> bytes:
    """Convert a validated NDWI array into deterministic PNG bytes.

    Args:
        ndwi: 2D NDWI array with float32/float64 dtype.
        colormap_normalization: Strategy for mapping NDWI to colormap.

    Returns:
        Binary PNG bytes.
    """
    return rgb_to_png_bytes(
        ndwi_to_rgb(ndwi, colormap_normalization=colormap_normalization)
    )
