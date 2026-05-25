"""Sentinel-1 context data for NDVI anomaly explanation.

Provides context flags from Sentinel-1 SAR data to explain NDVI
anomalies (e.g., wet soil, flooding). Sentinel-1 never produces
NDVI values — it only affects context and quality flags.

Architecture spec: docs/architecture/ndvi-system-evolution-phased-spec.md
Section 9 (Phase 4 - Fusion and Intelligence).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

logger = logging.getLogger(__name__)

S1_CONTEXT_FLAG_FIELDS = [
    "s1_wet_soil",
    "s1_flooding",
    "s1_rough_surface",
    "s1_urban_interference",
]


@dataclass
class Sentinel1Context:
    """Context flags derived from Sentinel-1 SAR data.

    These flags explain NDVI anomalies but never contribute to
    NDVI selection. Sentinel-1 is signal-only, not NDVI.
    """

    wet_soil: bool = False
    flooding: bool = False
    rough_surface: bool = False
    urban_interference: bool = False

    def to_flags(self) -> dict[str, bool]:
        """Convert context to quality flag dict.

        Returns:
            Dict with 's1_' prefix keys for flag merging.
        """
        return {
            "s1_wet_soil": self.wet_soil,
            "s1_flooding": self.flooding,
            "s1_rough_surface": self.rough_surface,
            "s1_urban_interference": self.urban_interference,
        }

    def has_any_signal(self) -> bool:
        """Check if any Sentinel-1 signal is active."""
        return any(
            [
                self.wet_soil,
                self.flooding,
                self.rough_surface,
                self.urban_interference,
            ]
        )


def fetch_sentinel1_context(
    farm_id: int,
    bucket_date: date,
) -> Sentinel1Context:
    """Fetch Sentinel-1 context for a farm and date.

    Stub: returns empty context. Implement upstream integration
    to fetch actual Sentinel-1 SAR data (e.g., from CDSE or
    Copernicus Data Space Ecosystem).

    Args:
        farm_id: The farm to fetch context for.
        bucket_date: The date bucket to fetch context for.

    Returns:
        Sentinel1Context with detected flags.
    """
    logger.debug(
        "s1_context.fetch farm=%s date=%s (stub: no upstream)",
        farm_id,
        bucket_date,
    )

    return Sentinel1Context()


def merge_s1_context_flags(
    quality_flags: dict[str, bool],
    s1_context: Sentinel1Context,
) -> dict[str, bool]:
    """Merge Sentinel-1 context flags into quality flags.

    Args:
        quality_flags: Existing quality flags dict.
        s1_context: Sentinel-1 context to merge.

    Returns:
        Updated quality flags with s1_ prefix keys merged.
    """
    merged = dict(quality_flags)
    merged.update(s1_context.to_flags())
    return merged


def detect_anomaly(
    ndvi_value: float | None,
    s1_context: Sentinel1Context,
    *,
    ndvi_threshold: float = 0.15,
) -> tuple[bool, str | None]:
    """Detect if an NDVI value is anomalous using Sentinel-1 context.

    Anomaly detection rules:
    - If NDVI is very low (< threshold) and Sentinel-1 indicates
      wet soil or flooding, flag as possible flooding.
    - If NDVI is unexpectedly high and Sentinel-1 indicates
      urban interference, flag as urban artifact.

    Args:
        ndvi_value: The selected NDVI value (may be None).
        s1_context: Sentinel-1 context for explanation.
        ndvi_threshold: NDVI threshold for anomaly detection.

    Returns:
        Tuple of (is_anomaly, anomaly_reason).
    """
    if ndvi_value is None:
        return False, None

    if ndvi_value < ndvi_threshold:
        if s1_context.flooding:
            return True, "possible_flooding"
        if s1_context.wet_soil:
            return True, "wet_soil_depression"

    if ndvi_value > (1.0 - ndvi_threshold) and s1_context.urban_interference:
        return True, "urban_artifact"

    return False, None
