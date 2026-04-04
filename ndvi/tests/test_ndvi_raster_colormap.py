from __future__ import annotations

from io import BytesIO

import numpy as np
import pytest
from PIL import Image

from ndvi.raster import png as png_module
from ndvi.raster.base import ColormapNormalization
from ndvi.raster.png import ndvi_to_png_bytes, ndvi_to_rgb, rgb_to_png_bytes


def _decode_png(png_bytes: bytes) -> np.ndarray:
    with Image.open(BytesIO(png_bytes)) as image:
        rgb_image = image.convert("RGB")
        return np.array(rgb_image, dtype=np.uint8)


def test_ndvi_to_rgb_maps_extremes_to_expected_color_directions() -> None:
    ndvi = np.array([[-1.0, 0.0, 1.0]], dtype=np.float32)

    rgb = ndvi_to_rgb(ndvi, ColormapNormalization.FIXED)

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

    png_bytes = ndvi_to_png_bytes(ndvi, ColormapNormalization.FIXED)

    assert png_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    decoded = _decode_png(png_bytes)
    assert decoded.shape == (2, 2, 3)


def test_ndvi_to_png_bytes_mixed_values_do_not_render_all_white() -> None:
    ndvi = np.array([[-1.0, -0.2], [0.4, 1.0]], dtype=np.float32)

    decoded = _decode_png(ndvi_to_png_bytes(ndvi, ColormapNormalization.FIXED))

    assert not np.all(decoded == 255)


def test_ndvi_to_rgb_maps_nan_to_zero_deterministically() -> None:
    ndvi = np.array([[np.nan, 0.1]], dtype=np.float32)

    rgb = ndvi_to_rgb(ndvi, ColormapNormalization.FIXED)
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


def test_ndvi_to_rgb_rejects_constant_ndvi_histogram() -> None:
    ndvi = np.full((2, 2), 0.2, dtype=np.float32)

    with pytest.raises(ValueError, match="no variation"):
        ndvi_to_rgb(ndvi, ColormapNormalization.HISTOGRAM)


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

    rgb = ndvi_to_rgb(ndvi, ColormapNormalization.FIXED)

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
        ndvi_to_rgb(ndvi, ColormapNormalization.FIXED)


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


# New tests for histogram normalization mode


def test_ndvi_to_rgb_histogram_stretches_realistic_range() -> None:
    """Histogram mode should stretch realistic NDVI range to full colormap."""
    # Typical vegetation NDVI values (0.4-0.6)
    ndvi = np.array([[0.4, 0.5], [0.55, 0.6]], dtype=np.float32)

    rgb_fixed = ndvi_to_rgb(ndvi, ColormapNormalization.FIXED)
    rgb_hist = ndvi_to_rgb(ndvi, ColormapNormalization.HISTOGRAM)

    # Fixed mode: all values map to green zone (0.7-0.8 normalized)
    assert rgb_fixed.mean() > 140  # Mostly green

    # Histogram mode: should use full color range
    assert rgb_hist.max() - rgb_hist.min() > 100  # More variation
    # Min value should be in red/orange zone
    assert rgb_hist.min() < 150
    # Max value should be in green zone
    assert rgb_hist.max() > 180


def test_ndvi_to_png_bytes_histogram_vs_fixed() -> None:
    """Histogram and fixed modes should produce different PNGs."""
    ndvi = np.array(
        [[0.42, 0.50], [0.55, 0.63]],
        dtype=np.float32,
    )

    png_fixed = ndvi_to_png_bytes(ndvi, ColormapNormalization.FIXED)
    png_hist = ndvi_to_png_bytes(ndvi, ColormapNormalization.HISTOGRAM)

    # Should produce different PNG bytes
    assert png_fixed != png_hist

    # Both should be valid PNGs
    assert png_fixed.startswith(b"\x89PNG\r\n\x1a\n")
    assert png_hist.startswith(b"\x89PNG\r\n\x1a\n")

    # Histogram should have more color variation
    decoded_fixed = _decode_png(png_fixed)
    decoded_hist = _decode_png(png_hist)

    assert decoded_fixed.shape == decoded_hist.shape == (2, 2, 3)
    # Histogram should utilize more of the colormap
    assert (
        decoded_hist.max() - decoded_hist.min()
        > decoded_fixed.max() - decoded_fixed.min()
    )


def test_ndvi_to_rgb_histogram_with_extreme_range() -> None:
    """Histogram mode should handle very narrow NDVI ranges."""
    # Very narrow range (0.01 variation)
    ndvi = np.array([[0.50, 0.505], [0.51, 0.502]], dtype=np.float32)

    rgb = ndvi_to_rgb(ndvi, ColormapNormalization.HISTOGRAM)

    # Should still produce valid RGB with variation
    assert rgb.dtype == np.uint8
    assert rgb.shape == (2, 2, 3)
    assert rgb.max() != rgb.min()  # Should have some variation


def test_ndvi_to_rgb_fixed_with_typical_vegetation() -> None:
    """Fixed mode should map typical vegetation to green zone."""
    ndvi = np.array([[0.4, 0.5], [0.6, 0.7]], dtype=np.float32)

    rgb = ndvi_to_rgb(ndvi, ColormapNormalization.FIXED)

    # All values should be in green/lime zone
    # Green channel should dominate
    assert np.all(rgb[:, :, 1] > rgb[:, :, 0])  # Green > Red
    assert np.all(rgb[:, :, 1] > rgb[:, :, 2])  # Green > Blue


def test_ndvi_to_rgb_histogram_preserves_spatial_pattern() -> None:
    """Histogram mode should preserve spatial patterns."""
    # Create a gradient pattern
    ndvi = np.linspace(0.3, 0.7, 100, dtype=np.float32).reshape(10, 10)

    rgb = ndvi_to_rgb(ndvi, ColormapNormalization.HISTOGRAM)

    # Should have smooth gradient (adjacent pixels similar)
    for i in range(9):
        for j in range(9):
            diff = abs(int(rgb[i, j].mean()) - int(rgb[i + 1, j + 1].mean()))
            assert diff < 50  # Adjacent pixels shouldn't differ too much
