from __future__ import annotations

from io import BytesIO

import numpy as np
import pytest
from PIL import Image

from ndvi.raster.png import ndvi_to_png_bytes, ndvi_to_rgb


def _decode_png(png_bytes: bytes) -> np.ndarray:
    with Image.open(BytesIO(png_bytes)) as image:
        rgb_image = image.convert("RGB")
        return np.array(rgb_image, dtype=np.uint8)


def test_ndvi_to_rgb_maps_extremes_to_expected_color_directions() -> None:
    ndvi = np.array([[-1.0, 0.0, 1.0]], dtype=np.float32)

    rgb = ndvi_to_rgb(ndvi)

    assert rgb.dtype == np.uint8
    assert rgb.shape == (1, 3, 3)

    min_pixel = rgb[0, 0]
    mid_pixel = rgb[0, 1]
    max_pixel = rgb[0, 2]

    assert min_pixel[0] > min_pixel[1]
    assert min_pixel[0] > min_pixel[2]
    assert mid_pixel[0] >= 200
    assert mid_pixel[1] >= 180
    assert max_pixel[1] > max_pixel[0]
    assert max_pixel[1] > max_pixel[2]


def test_ndvi_to_png_bytes_returns_valid_png_bytes() -> None:
    ndvi = np.array([[0.0, 0.5], [1.0, -1.0]], dtype=np.float32)

    png_bytes = ndvi_to_png_bytes(ndvi)

    assert png_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    decoded = _decode_png(png_bytes)
    assert decoded.shape == (2, 2, 3)


def test_ndvi_to_png_bytes_mixed_values_do_not_render_all_white() -> None:
    ndvi = np.array([[-1.0, -0.2], [0.4, 1.0]], dtype=np.float32)

    decoded = _decode_png(ndvi_to_png_bytes(ndvi))

    assert not np.all(decoded == 255)


def test_ndvi_to_rgb_maps_nan_to_zero_deterministically() -> None:
    ndvi = np.array([[np.nan, 0.1]], dtype=np.float32)

    rgb = ndvi_to_rgb(ndvi)

    assert np.array_equal(rgb[0, 0], rgb[0, 1])


def test_ndvi_to_rgb_rejects_non_float_input() -> None:
    ndvi = np.array([[0, 1]], dtype=np.int32)

    with pytest.raises(TypeError, match="float32 or float64"):
        ndvi_to_rgb(ndvi)
