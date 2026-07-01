"""Spectral index formula registry.

Maps index type names to their formulae, required bands, ranges,
sensor-specific band mappings, and metadata.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import yaml

IndexDefinition = dict[str, Any]


def _ndvi_fn(nir: np.ndarray, red: np.ndarray) -> np.ndarray:
    return (nir.astype(np.float32) - red.astype(np.float32)) / (
        nir.astype(np.float32) + red.astype(np.float32)
    )


def _ndwi_fn(nir: np.ndarray, green: np.ndarray) -> np.ndarray:
    return (green.astype(np.float32) - nir.astype(np.float32)) / (
        green.astype(np.float32) + nir.astype(np.float32)
    )


def _ndmi_fn(nir: np.ndarray, swir1: np.ndarray) -> np.ndarray:
    return (nir.astype(np.float32) - swir1.astype(np.float32)) / (
        nir.astype(np.float32) + swir1.astype(np.float32)
    )


def _rvi_fn(vv: np.ndarray, vh: np.ndarray) -> np.ndarray:
    denom = vv.astype(np.float32) + vh.astype(np.float32)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(denom > 0, (4 * vh.astype(np.float32)) / denom, 0.0)


def _s1_smi_fn(vv: np.ndarray, vh: np.ndarray) -> np.ndarray:
    vv_f = vv.astype(np.float32)
    vh_f = vh.astype(np.float32)
    vv_db = 10.0 * np.log10(np.maximum(vv_f, 1e-10))
    vh_db = 10.0 * np.log10(np.maximum(vh_f, 1e-10))
    calib = _load_s1_smi_calibration()
    default_calib = calib.get("s1_smi_coefficients", {}).get("default", {}).get("ascending", {})
    alpha = default_calib.get("alpha", 0.70)
    beta = default_calib.get("beta", -0.30)
    gamma = default_calib.get("gamma", 0.50)
    return alpha * vv_db + beta * vh_db + gamma


def _load_s1_smi_calibration() -> dict[str, Any]:
    path = (
        Path(__file__).resolve().parent.parent
        / "thresholds"
        / "s1_smi_calibration.yaml"
    )
    if path.exists():
        with open(path) as f:
            return yaml.safe_load(f) or {}
    return {}


FORMULA_REGISTRY: dict[str, IndexDefinition] = {
    "NDVI": {
        "name": "NDVI",
        "formula": _ndvi_fn,
        "bands": ["nir", "red"],
        "range": (-1.0, 1.0),
        "default_colormap": "RdYlGn",
        "default_min": -0.2,
        "default_max": 0.8,
        "sensor_band_map": {
            "sentinel2_l2a": {"nir": "B08_10m", "red": "B04_10m"},
            "landsat89_l2": {"nir": "B5", "red": "B4"},
            "modis_09ga": {"nir": "sur_refl_b02", "red": "sur_refl_b01"},
        },
        "scl_mask": [0, 1, 2, 3, 8, 9, 10, 11],
        "description": "Normalized Difference Vegetation Index",
    },
    "NDWI": {
        "name": "NDWI",
        "formula": _ndwi_fn,
        "bands": ["nir", "green"],
        "range": (-1.0, 1.0),
        "default_colormap": "BrBG",
        "default_min": -0.5,
        "default_max": 0.5,
        "sensor_band_map": {
            "sentinel2_l2a": {"nir": "B08_10m", "green": "B03_10m"},
            "landsat89_l2": {"nir": "B5", "green": "B3"},
            "modis_09ga": {"nir": "sur_refl_b02", "green": "sur_refl_b04"},
        },
        "scl_mask": [0, 1, 2, 3, 8, 9, 10, 11],
        "description": "Normalized Difference Water Index",
    },
    "NDMI": {
        "name": "NDMI",
        "formula": _ndmi_fn,
        "bands": ["nir", "swir1"],
        "range": (-1.0, 1.0),
        "default_colormap": "YlOrRd",
        "default_min": -0.2,
        "default_max": 0.8,
        "sensor_band_map": {
            "sentinel2_l2a": {"nir": "B08_10m", "swir1": "B11_20m"},
            "landsat89_l2": {"nir": "B5", "swir1": "B6"},
            "modis_09ga": {"nir": "sur_refl_b02", "swir1": "sur_refl_b06"},
        },
        "scl_mask": [0, 1, 2, 3, 8, 9, 10, 11],
        "description": "Normalized Difference Moisture Index",
    },
    "RVI": {
        "name": "RVI",
        "formula": _rvi_fn,
        "bands": ["vv", "vh"],
        "range": (0.0, 1.0),
        "default_colormap": "YlGn",
        "default_min": 0.0,
        "default_max": 1.0,
        "sensor_band_map": {
            "sentinel1_rtc": {"vv": "VV", "vh": "VH"},
        },
        "description": (
            "Radar Vegetation Index — measures canopy structural"
            " complexity using C-band SAR polarimetry"
        ),
    },
    "S1_SMI": {
        "name": "S1_SMI",
        "formula": _s1_smi_fn,
        "bands": ["vv", "vh"],
        "range": (-50.0, 0.0),
        "default_colormap": "Blues",
        "default_min": -30.0,
        "default_max": -5.0,
        "sensor_band_map": {
            "sentinel1_rtc": {"vv": "VV", "vh": "VH"},
        },
        "description": (
            "Sentinel-1 Soil Moisture Index — empirical retrieval of"
            " near-surface soil moisture from C-band SAR backscatter"
        ),
    },
}


def get_formula(index_type: str) -> IndexDefinition:
    """Return the formula definition for the given index type."""
    formula = FORMULA_REGISTRY.get(index_type)
    if formula is None:
        raise KeyError(f"Unknown index type: {index_type}")
    return formula


def compute_index(index_type: str, **bands: np.ndarray) -> np.ndarray:
    """Compute a spectral index using bands keyed by abstract band name.

    Args:
        index_type: e.g. "NDVI", "NDWI", "NDMI"
        **bands: numpy arrays keyed by band name (e.g. nir=..., red=...)

    Returns:
        Computed index array as float32 with NaN for invalid pixels.
    """
    formula = get_formula(index_type)
    required = formula["bands"]
    resolved = {}
    for band in required:
        arr = bands.get(band)
        if arr is None:
            raise ValueError(
                f"Missing required band '{band}' for {index_type}"
            )
        resolved[band] = arr
    with np.errstate(divide="ignore", invalid="ignore"):
        result = formula["formula"](**resolved)
    return result.astype(np.float32)
