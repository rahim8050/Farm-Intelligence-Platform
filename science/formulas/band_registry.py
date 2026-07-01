"""Band registry mapping sensor-specific asset keys to abstract band names.

Provides a single source of truth for resolving band asset keys across
different satellite sensors and collections.
"""

from __future__ import annotations

from typing import Final

BAND_REGISTRY: Final[dict[str, dict[str, str]]] = {
    "sentinel2_l2a": {
        "red": "B04_10m",
        "green": "B03_10m",
        "nir": "B08_10m",
        "swir1": "B11_20m",
        "swir2": "B12_20m",
        "scl": "SCL",
    },
    "landsat89_l2": {
        "red": "B4",
        "green": "B3",
        "nir": "B5",
        "swir1": "B6",
        "swir2": "B7",
    },
    "modis_09ga": {
        "nir": "sur_refl_b02",
        "swir1": "sur_refl_b06",
        "qa": "state_1km",
    },
    "sentinel1_rtc": {
        "vv": "VV",
        "vh": "VH",
        "angle": "local_incidence_angle",
    },
}


def get_band_asset_key(sensor_key: str, band_name: str) -> str:
    """Resolve an abstract band name to a sensor-specific asset key.

    Args:
        sensor_key: e.g. "sentinel2_l2a", "landsat89_l2", "modis_09ga"
        band_name: abstract band name e.g. "nir", "red", "swir1"

    Returns:
        Sensor-specific asset key string.

    Raises:
        KeyError: if sensor or band is not registered.
    """
    sensor = BAND_REGISTRY.get(sensor_key)
    if sensor is None:
        raise KeyError(f"Unknown sensor key: {sensor_key}")
    asset_key = sensor.get(band_name)
    if asset_key is None:
        raise KeyError(
            f"Band '{band_name}' not defined for sensor '{sensor_key}'"
        )
    return asset_key
