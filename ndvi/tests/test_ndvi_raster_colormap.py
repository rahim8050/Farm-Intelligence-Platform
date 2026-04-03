from __future__ import annotations

from io import BytesIO

import numpy as np
import pytest
from PIL import Image

from ndvi.raster import png as png_module
from ndvi.raster.png import ndvi_to_png_bytes, ndvi_to_rgb, rgb_to_png_bytes


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
    try:
        colormaps = png_module._load_matplotlib_colormaps()
    except ImportError:
        zero_color = png_module._fallback_rdylgn_bytes(
            np.array([[0.5]], dtype=np.float32)
        )[0, 0]
    else:
        zero_color = colormaps[png_module.NDVI_COLORMAP_NAME](
            np.array([[0.5]], dtype=np.float32),
            bytes=True,
        )[0, 0, :3]

    assert np.array_equal(rgb[0, 0], zero_color)
    assert not np.array_equal(rgb[0, 0], rgb[0, 1])


def test_ndvi_to_rgb_rejects_non_float_input() -> None:
    ndvi = np.array([[0, 1]], dtype=np.int32)

    with pytest.raises(TypeError, match="float32 or float64"):
        ndvi_to_rgb(ndvi)


def test_ndvi_to_rgb_rejects_non_2d_input() -> None:
    ndvi = np.array([0.1, 0.2], dtype=np.float32)

    with pytest.raises(ValueError, match="two-dimensional"):
        ndvi_to_rgb(ndvi)


def test_ndvi_to_rgb_rejects_empty_input() -> None:
    ndvi = np.empty((0, 0), dtype=np.float32)

    with pytest.raises(ValueError, match="must not be empty"):
        ndvi_to_rgb(ndvi)


def test_ndvi_to_rgb_rejects_constant_ndvi() -> None:
    ndvi = np.full((2, 2), 0.2, dtype=np.float32)

    with pytest.raises(ValueError, match="no variation"):
        ndvi_to_rgb(ndvi)


def test_ndvi_to_rgb_uses_fallback_when_matplotlib_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ndvi = np.array([[-1.0, 0.0], [0.5, 1.0]], dtype=np.float32)

    def raise_import_error() -> object:
        raise ImportError("matplotlib unavailable")

    monkeypatch.setattr(
        png_module,
        "_load_matplotlib_colormaps",
        raise_import_error,
    )

    rgb = ndvi_to_rgb(ndvi)

    assert rgb.dtype == np.uint8
    assert rgb.shape == (2, 2, 3)


def test_ndvi_to_rgb_rejects_invalid_colormap_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ndvi = np.array([[-1.0, 0.0], [0.5, 1.0]], dtype=np.float32)

    class InvalidColormap:
        def __call__(
            self, normalized: np.ndarray, *, bytes: bool
        ) -> np.ndarray:
            assert bytes is True
            return np.zeros((*normalized.shape, 4), dtype=np.float32)

    monkeypatch.setattr(
        png_module,
        "_load_matplotlib_colormaps",
        lambda: {png_module.NDVI_COLORMAP_NAME: InvalidColormap()},
    )

    with pytest.raises(TypeError, match="RGB uint8 image"):
        ndvi_to_rgb(ndvi)


def test_rgb_to_png_bytes_rejects_invalid_shape() -> None:
    rgb = np.zeros((2, 2), dtype=np.uint8)

    with pytest.raises(ValueError, match=r"\(H, W, 3\)"):
        rgb_to_png_bytes(rgb)


def test_rgb_to_png_bytes_rejects_empty_input() -> None:
    rgb = np.empty((0, 0, 3), dtype=np.uint8)

    with pytest.raises(ValueError, match="must not be empty"):
        rgb_to_png_bytes(rgb)


def test_rgb_to_png_bytes_rejects_non_uint8_input() -> None:
    rgb = np.zeros((2, 2, 3), dtype=np.float32)

    with pytest.raises(TypeError, match="must use uint8"):
        rgb_to_png_bytes(rgb)
