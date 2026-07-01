"""Tests for science/formulas modules.

Covers:
- FORMULA_REGISTRY entries (NDVI, NDWI, NDMI)
- get_formula function
- compute_index function
- BAND_REGISTRY entries
- get_band_asset_key function
"""

from __future__ import annotations

import numpy as np
import pytest

from science.formulas.band_registry import BAND_REGISTRY, get_band_asset_key
from science.formulas.registry import (
    FORMULA_REGISTRY,
    compute_index,
    get_formula,
)


class TestFormulaRegistry:
    """FORMULA_REGISTRY must contain all three index types."""

    def test_ndvi_in_registry(self) -> None:
        entry = FORMULA_REGISTRY.get("NDVI")
        assert entry is not None
        assert entry["name"] == "NDVI"
        assert "nir" in entry["bands"]
        assert "red" in entry["bands"]
        assert callable(entry["formula"])

    def test_ndwi_in_registry(self) -> None:
        entry = FORMULA_REGISTRY.get("NDWI")
        assert entry is not None
        assert entry["name"] == "NDWI"
        assert "nir" in entry["bands"]
        assert "green" in entry["bands"]
        assert callable(entry["formula"])

    def test_ndmi_in_registry(self) -> None:
        entry = FORMULA_REGISTRY.get("NDMI")
        assert entry is not None
        assert entry["name"] == "NDMI"
        assert "nir" in entry["bands"]
        assert "swir1" in entry["bands"]
        assert callable(entry["formula"])

    def test_all_entries_have_required_keys(self) -> None:
        required = {"name", "formula", "bands", "range", "sensor_band_map"}
        for name, entry in FORMULA_REGISTRY.items():
            assert required.issubset(entry.keys()), f"{name} missing keys"

    def test_get_formula_returns_entry(self) -> None:
        entry = get_formula("NDMI")
        assert entry["name"] == "NDMI"

    def test_get_formula_raises_for_unknown(self) -> None:
        with pytest.raises(KeyError):
            get_formula("UNKNOWN")


class TestComputeIndex:
    """compute_index function works for each formula."""

    def test_compute_ndmi(self) -> None:
        nir = np.array([0.5, 0.6], dtype=np.float32)
        swir1 = np.array([0.2, 0.3], dtype=np.float32)
        result = compute_index(index_type="NDMI", nir=nir, swir1=swir1)
        expected = (nir - swir1) / (nir + swir1)
        np.testing.assert_allclose(result, expected)

    def test_compute_ndvi(self) -> None:
        nir = np.array([0.5, 0.6], dtype=np.float32)
        red = np.array([0.1, 0.2], dtype=np.float32)
        result = compute_index(index_type="NDVI", nir=nir, red=red)
        expected = (nir - red) / (nir + red)
        np.testing.assert_allclose(result, expected)

    def test_compute_ndwi(self) -> None:
        nir = np.array([0.5, 0.6], dtype=np.float32)
        green = np.array([0.3, 0.4], dtype=np.float32)
        result = compute_index(index_type="NDWI", nir=nir, green=green)
        expected = (green - nir) / (green + nir)
        np.testing.assert_allclose(result, expected)

    def test_compute_rvi(self) -> None:
        vv = np.array([0.5, 0.6], dtype=np.float32)
        vh = np.array([0.1, 0.2], dtype=np.float32)
        result = compute_index(index_type="RVI", vv=vv, vh=vh)
        expected = (4 * vh) / (vv + vh)
        np.testing.assert_allclose(result, expected)

    def test_compute_s1_smi(self) -> None:
        vv = np.array([0.5, 0.6], dtype=np.float32)
        vh = np.array([0.1, 0.2], dtype=np.float32)
        result = compute_index(index_type="S1_SMI", vv=vv, vh=vh)
        vv_db = 10.0 * np.log10(vv)
        vh_db = 10.0 * np.log10(vh)
        expected = 0.70 * vv_db - 0.30 * vh_db + 0.50
        np.testing.assert_allclose(result, expected)

    def test_compute_ndmi_handles_zeros(self) -> None:
        nir = np.array([0.0, 0.0], dtype=np.float32)
        swir1 = np.array([0.0, 0.0], dtype=np.float32)
        result = compute_index(index_type="NDMI", nir=nir, swir1=swir1)
        assert np.all(np.isnan(result))

    def test_compute_index_raises_for_missing_band(self) -> None:
        nir = np.array([0.5], dtype=np.float32)
        with pytest.raises(ValueError):
            compute_index(index_type="NDMI", nir=nir)

    def test_compute_index_raises_for_unknown_type(self) -> None:
        with pytest.raises(KeyError):
            compute_index(index_type="FAKE", nir=np.array([0.5]))


class TestBandRegistry:
    """BAND_REGISTRY and get_band_asset_key."""

    def test_sentinel2_has_swir1(self) -> None:
        assert "swir1" in BAND_REGISTRY["sentinel2_l2a"]
        assert BAND_REGISTRY["sentinel2_l2a"]["swir1"] == "B11_20m"

    def test_landsat_has_swir1(self) -> None:
        assert "swir1" in BAND_REGISTRY["landsat89_l2"]
        assert BAND_REGISTRY["landsat89_l2"]["swir1"] == "B6"

    def test_modis_has_swir1(self) -> None:
        assert "swir1" in BAND_REGISTRY["modis_09ga"]
        assert BAND_REGISTRY["modis_09ga"]["swir1"] == "sur_refl_b06"

    def test_get_band_asset_key_swir1(self) -> None:
        key = get_band_asset_key("sentinel2_l2a", "swir1")
        assert key == "B11_20m"

    def test_get_band_asset_key_nir(self) -> None:
        key = get_band_asset_key("sentinel2_l2a", "nir")
        assert key == "B08_10m"

    def test_get_band_asset_key_raises_for_unknown_sensor(self) -> None:
        with pytest.raises(KeyError):
            get_band_asset_key("unknown_sensor", "nir")

    def test_get_band_asset_key_raises_for_unknown_band(self) -> None:
        with pytest.raises(KeyError):
            get_band_asset_key("sentinel2_l2a", "unknown_band")

    def test_all_optical_sensors_have_nir(self) -> None:
        for sensor_key, bands in BAND_REGISTRY.items():
            if "sentinel1" in sensor_key:
                continue
            assert "nir" in bands, f"{sensor_key} missing nir"
