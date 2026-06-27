"""NDMI-specific colormap definition.

NDMI uses a diverging colormap (brown -> white -> green) to represent
dry -> moist -> wet conditions. The default colormap is "BrBG"
(Brown-Blue-Green) matching the NDWI pattern but with
NDMI-appropriate default range.

Architecture: the COLORMAP_REGISTRY maps index types to their colormap
configuration, mirroring the FORMULA_REGISTRY pattern in science/formulas/.
"""

from __future__ import annotations

from typing import Any

import numpy as np

# ── NDMI colormap constants ─────────────────────────────────────────────

NDMI_COLORMAP_NAME: str = "BrBG"
"""Matplotlib colormap name for NDMI visualization.

BrBG (Brown-Blue-Green) diverging colormap shows:
- Brown/tan: dry conditions (negative NDMI)
- White/neutral: moderate moisture (NDMI near 0)
- Blue/green: wet conditions (positive NDMI)
"""

NDMI_DEFAULT_MIN: float = -0.2
"""Default minimum NDMI value for colormap mapping.

Values below this are clamped to the minimum colormap color.
Based on typical NDMI range for agricultural monitoring.
"""

NDMI_DEFAULT_MAX: float = 0.8
"""Default maximum NDMI value for colormap mapping.

Values above this are clamped to the maximum colormap color.
Based on typical NDMI range for agricultural monitoring.
"""

# BrBG control points (brown -> white -> blue/green)
# Same control points as NDWI but with different default range
NDMI_BRBG_CONTROL_POINTS: np.ndarray = np.array(
    [
        [84, 48, 5],  # Dark brown (dry)
        [140, 81, 10],  # Brown
        [191, 129, 45],  # Tan
        [223, 194, 125],  # Light tan
        [246, 232, 195],  # Pale tan
        [245, 245, 245],  # White (neutral)
        [199, 234, 229],  # Pale cyan
        [128, 205, 193],  # Light teal
        [53, 151, 143],  # Teal
        [1, 102, 94],  # Dark teal
        [0, 60, 48],  # Very dark teal (wet)
    ],
    dtype=np.float32,
)

# ── Registry ────────────────────────────────────────────────────────────

COLORMAP_REGISTRY: dict[str, dict[str, Any]] = {
    "NDMI": {
        "colormap_name": NDMI_COLORMAP_NAME,
        "default_min": NDMI_DEFAULT_MIN,
        "default_max": NDMI_DEFAULT_MAX,
        "control_points": NDMI_BRBG_CONTROL_POINTS,
        "description": (
            "Diverging colormap (BrBG) for NDMI: brown=low moisture "
            "(negative NDMI), white=moderate, blue/green=high moisture "
            "(positive NDMI)"
        ),
    },
}
"""Registry mapping index type to colormap configuration.

Each entry contains:
- colormap_name: Matplotlib colormap name
- default_min: Default lower bound for the index
- default_max: Default upper bound for the index
- control_points: Fallback control points when matplotlib is unavailable
- description: Human-readable description
"""
