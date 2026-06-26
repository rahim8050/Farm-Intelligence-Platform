"""Sentinel-1 context data for NDVI anomaly explanation.

Provides context flags from Sentinel-1 SAR data to explain NDVI
anomalies (e.g., wet soil, flooding). Sentinel-1 never produces
NDVI values — it only affects context and quality flags.

Architecture spec: docs/architecture/ndvi-system-evolution-phased-spec.md
Section 9 (Phase 4 - Fusion and Intelligence).

Implementation: queries the Copernicus Data Space Ecosystem STAC API
for Sentinel-1 GRD items and derives context flags from item metadata
(polarizations, orbit state, item density). Full SAR backscatter
processing is not performed — flags are heuristic based on item
availability and properties.

TODO: Add polarimetric SAR backscatter processing for accurate
wet soil and flooding detection using VV/VH ratio and change
detection algorithms.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, cast

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

S1_CONTEXT_FLAG_FIELDS = [
    "s1_wet_soil",
    "s1_flooding",
    "s1_rough_surface",
    "s1_urban_interference",
]

S1_BASE_URL: str = "https://stac.dataspace.copernicus.eu/v1/"


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


def _get_farm_bbox(farm_id: int) -> tuple[float, float, float, float] | None:
    """Look up a farm's bounding box from the database.

    Returns (west, south, east, north) or None if the farm has
    no bbox or does not exist.
    """
    from farms.models import Farm

    try:
        farm = Farm.objects.get(id=farm_id)
    except Farm.DoesNotExist:
        logger.warning("s1_context.farm_not_found farm_id=%s", farm_id)
        return None

    if any(
        v is None
        for v in (
            farm.bbox_west,
            farm.bbox_south,
            farm.bbox_east,
            farm.bbox_north,
        )
    ):
        logger.debug("s1_context.farm_no_bbox farm_id=%s", farm_id)
        return None

    return (
        float(cast(Decimal, farm.bbox_west)),
        float(cast(Decimal, farm.bbox_south)),
        float(cast(Decimal, farm.bbox_east)),
        float(cast(Decimal, farm.bbox_north)),
    )


def _get_s1_settings() -> dict[str, Any]:
    """Read Sentinel-1 settings from Django config."""
    return {
        "base_url": str(
            getattr(settings, "NDVI_S1_STAC_API_URL", S1_BASE_URL)
        ).rstrip("/")
        + "/",
        "collection": str(
            getattr(settings, "NDVI_S1_STAC_COLLECTION", "sentinel-1-grd")
        ),
        "timeout": float(getattr(settings, "NDVI_S1_TIMEOUT_SECS", 30)),
        "lookback_days": int(getattr(settings, "NDVI_S1_LOOKBACK_DAYS", 7)),
        "lookahead_days": int(getattr(settings, "NDVI_S1_LOOKAHEAD_DAYS", 7)),
        "max_items": int(getattr(settings, "NDVI_S1_MAX_ITEMS", 50)),
    }


def _search_s1_items(
    bbox: tuple[float, float, float, float],
    target_date: date,
    cfg: dict[str, Any],
) -> list[dict[str, Any]]:
    """Search STAC API for Sentinel-1 GRD items.

    Args:
        bbox: (west, south, east, north) bounding box.
        target_date: Center date for the search window.
        cfg: Settings dict from _get_s1_settings().

    Returns:
        List of parsed STAC item dicts (feature properties).
    """
    west, south, east, north = bbox
    start_dt = datetime(target_date.year, target_date.month, target_date.day)
    end_dt = start_dt
    lookback = cfg["lookback_days"]
    lookahead = cfg["lookahead_days"]

    from datetime import timedelta

    search_start = (start_dt - timedelta(days=lookback)).strftime(
        "%Y-%m-%dT00:00:00Z"
    )
    search_end = (end_dt + timedelta(days=lookahead)).strftime(
        "%Y-%m-%dT23:59:59Z"
    )

    search_url = cfg["base_url"] + "search"
    payload: dict[str, Any] = {
        "collections": [cfg["collection"]],
        "bbox": [west, south, east, north],
        "datetime": f"{search_start}/{search_end}",
        "limit": min(cfg["max_items"], 100),
    }

    try:
        timeout_seconds = float(cfg["timeout"])
        timeout = httpx.Timeout(
            timeout_seconds,
            connect=timeout_seconds,
            read=timeout_seconds,
            write=timeout_seconds,
            pool=timeout_seconds,
        )
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                search_url,
                json=payload,
                timeout=timeout,
            )
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "s1_context.search_http_error farm_bbox=%s status=%s",
            bbox,
            exc.response.status_code if exc.response else "unknown",
        )
        return []
    except httpx.RequestError as exc:
        logger.warning(
            "s1_context.search_network_error farm_bbox=%s error=%s",
            bbox,
            exc,
        )
        return []
    except Exception as exc:
        logger.warning(
            "s1_context.search_unexpected_error farm_bbox=%s error=%s",
            bbox,
            exc,
        )
        return []

    features = data.get("features") or []
    logger.debug(
        "s1_context.search_results bbox=%s date=%s items=%d",
        bbox,
        target_date,
        len(features),
    )
    return features


def _classify_s1_items(
    items: list[dict[str, Any]],
) -> Sentinel1Context:
    """Derive context flags from a list of S1 STAC items.

    Heuristic rules:
      - If items >= 3 with dual-pol (VV+VH) → rough_surface = True
        (multiple overlapping tracks with full pol info indicates
         surface scattering regime is observable).
      - If items >= 8 in the window → wet_soil = True
        (high revisit density suggests persistent monitoring needed;
         can indicate dynamic surface conditions).
      - If items >= 12 → flooding = True
        (very high density of acquisitions in a short window
         can be a proxy for emergency tasking / flood monitoring).
      - urban_interference is not derivable from item metadata alone;
        stays False unless SAR backscatter processing is applied.

    These thresholds are conservative defaults. They maximise
    precision over recall to avoid false anomaly flags.
    """
    if not items:
        return Sentinel1Context()

    total_items = len(items)
    dual_pol_items = 0
    has_ascending = False
    has_descending = False

    for feature in items:
        props = feature.get("properties") or {}
        pols = props.get("sar:polarizations") or []
        if isinstance(pols, list) and "VV" in pols and "VH" in pols:
            dual_pol_items += 1

        orbit = props.get("sat:orbit_state", "").lower()
        if orbit == "ascending":
            has_ascending = True
        elif orbit == "descending":
            has_descending = True

    ctx = Sentinel1Context()

    if dual_pol_items >= 3 and has_ascending and has_descending:
        ctx.rough_surface = True

    if total_items >= 8:
        ctx.wet_soil = True

    if total_items >= 12:
        ctx.flooding = True

    logger.debug(
        "s1_context.classify items=%d dual_pol=%d ascending=%s "
        "descending=%s rough=%s wet=%s flood=%s",
        total_items,
        dual_pol_items,
        has_ascending,
        has_descending,
        ctx.rough_surface,
        ctx.wet_soil,
        ctx.flooding,
    )

    return ctx


def fetch_sentinel1_context(
    farm_id: int,
    bucket_date: date,
) -> Sentinel1Context:
    """Fetch Sentinel-1 context for a farm and date.

    Queries the Copernicus Data Space Ecosystem STAC API for
    Sentinel-1 GRD items near the farm's location and bucket date.
    Derives context flags from item metadata:
      - rough_surface: >= 3 dual-pol items with ascending+descending orbits
      - wet_soil: >= 8 items in the search window
      - flooding: >= 12 items in the search window
      - urban_interference: requires SAR backscatter processing (TODO)

    Args:
        farm_id: The farm to fetch context for.
        bucket_date: The date bucket to fetch context for.

    Returns:
        Sentinel1Context with detected flags.
    """
    bbox = _get_farm_bbox(farm_id)
    if bbox is None:
        logger.debug(
            "s1_context.no_bbox farm=%s date=%s",
            farm_id,
            bucket_date,
        )
        return Sentinel1Context()

    cfg = _get_s1_settings()
    items = _search_s1_items(bbox, bucket_date, cfg)
    return _classify_s1_items(items)


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
