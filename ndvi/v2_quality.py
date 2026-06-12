"""V2 Quality Engine for NDVI observations.

Converts raw V1 observations into decision-grade V2 observations
with confidence scoring, temporal smoothing, and explicit null behavior.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

from django.conf import settings
from django.db import transaction

from ndvi.metrics import (
    ndvi_v2_confidence_bucket,
    ndvi_v2_low_confidence_total,
    ndvi_v2_null_output_total,
    ndvi_v2_observation_total,
)
from ndvi.models import NdviDerivedObservation, NdviObservation

logger = logging.getLogger(__name__)

SOURCE_WEIGHTS: dict[str, float] = {
    "sentinel-2": 1.00,
    "sentinelhub": 1.00,
    "stac": 1.00,
    "landsat": 0.80,
    "modis": 0.60,
}

DEFAULT_SOURCE_WEIGHT = 0.60
DEFAULT_ROLLING_WINDOW = 5
DEFAULT_OUTLIER_THRESHOLD = 0.15
DEFAULT_ACCEPT_THRESHOLD = 0.75
DEFAULT_LOW_CONFIDENCE_THRESHOLD = 0.50
DEFAULT_VALID_PIXEL_REJECT = 0.30
DEFAULT_RECENCY_MAX_DAYS = 14
DEFAULT_TEMPORAL_DEVIATION = 0.20
DEFAULT_MIN_SMOOTH_VALUES = 3
DEFAULT_MIN_ROLLING_CONTEXT = 3
DEFAULT_MAX_CONFIDENCE_WITHOUT_CONTEXT = 0.49

CONFIDENCE_WEIGHTS = {
    "source": 0.30,
    "cloud": 0.25,
    "valid_pixel": 0.25,
    "recency": 0.10,
    "temporal": 0.10,
}


@dataclass
class ConfidenceComponents:
    """Breakdown of confidence formula components."""

    source_weight: float = 0.0
    cloud_weight: float = 0.0
    valid_pixel_weight: float = 0.0
    recency_weight: float = 0.0
    temporal_consistency_weight: float = 0.0

    def to_dict(self) -> dict[str, float]:
        return {
            "source_weight": round(self.source_weight, 4),
            "cloud_weight": round(self.cloud_weight, 4),
            "valid_pixel_weight": round(self.valid_pixel_weight, 4),
            "recency_weight": round(self.recency_weight, 4),
            "temporal_consistency_weight": round(
                self.temporal_consistency_weight, 4
            ),
        }


@dataclass
class V2Result:
    """Output of the V2 quality engine."""

    selected_ndvi: float | None
    smoothed_ndvi: float | None
    confidence: float
    confidence_components: ConfidenceComponents
    quality_flags: dict[str, bool]
    is_null: bool
    null_reason: str | None


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    mid = n // 2
    if n % 2 == 0:
        return (sorted_vals[mid - 1] + sorted_vals[mid]) / 2.0
    return sorted_vals[mid]


def get_rolling_window_size() -> int:
    return int(
        getattr(settings, "NDVI_V2_ROLLING_WINDOW", DEFAULT_ROLLING_WINDOW)
    )


def get_outlier_threshold() -> float:
    return float(
        getattr(
            settings,
            "NDVI_V2_OUTLIER_THRESHOLD",
            DEFAULT_OUTLIER_THRESHOLD,
        )
    )


def get_accept_threshold() -> float:
    return float(
        getattr(settings, "NDVI_V2_ACCEPT_THRESHOLD", DEFAULT_ACCEPT_THRESHOLD)
    )


def get_low_confidence_threshold() -> float:
    return float(
        getattr(
            settings,
            "NDVI_V2_LOW_CONFIDENCE_THRESHOLD",
            DEFAULT_LOW_CONFIDENCE_THRESHOLD,
        )
    )


def get_valid_pixel_reject_threshold() -> float:
    return float(
        getattr(
            settings,
            "NDVI_V2_VALID_PIXEL_REJECT",
            DEFAULT_VALID_PIXEL_REJECT,
        )
    )


def get_recency_max_days() -> int:
    return int(
        getattr(settings, "NDVI_V2_RECENCY_MAX_DAYS", DEFAULT_RECENCY_MAX_DAYS)
    )


def get_temporal_deviation() -> float:
    return float(
        getattr(
            settings,
            "NDVI_V2_TEMPORAL_DEVIATION",
            DEFAULT_TEMPORAL_DEVIATION,
        )
    )


def get_min_smooth_values() -> int:
    return int(
        getattr(
            settings,
            "NDVI_V2_MIN_SMOOTH_VALUES",
            DEFAULT_MIN_SMOOTH_VALUES,
        )
    )


def get_min_rolling_context() -> int:
    return int(
        getattr(
            settings,
            "NDVI_V2_MIN_ROLLING_CONTEXT",
            DEFAULT_MIN_ROLLING_CONTEXT,
        )
    )


def get_max_confidence_without_context() -> float:
    return float(
        getattr(
            settings,
            "NDVI_V2_MAX_CONFIDENCE_WITHOUT_CONTEXT",
            DEFAULT_MAX_CONFIDENCE_WITHOUT_CONTEXT,
        )
    )


def _get_source_weight(engine: str) -> float:
    return SOURCE_WEIGHTS.get(engine.lower(), DEFAULT_SOURCE_WEIGHT)


def _compute_cloud_weight(cloud_fraction: float | None) -> float:
    if cloud_fraction is None:
        return 0.5
    return 1.0 - _clamp(cloud_fraction)


def _compute_valid_pixel_weight(
    valid_pixel_fraction: float | None,
) -> float:
    if valid_pixel_fraction is None:
        return 0.5
    return _clamp(valid_pixel_fraction)


def _compute_recency_weight(
    acquisition_at: Any,
    bucket_date: date,
) -> float:
    max_days = get_recency_max_days()
    if acquisition_at is None:
        ref_date = bucket_date
    else:
        ref_date = acquisition_at.date()
    age_days = (bucket_date - ref_date).days
    if age_days < 0:
        age_days = 0
    return max(0.0, 1.0 - age_days / max_days)


def _compute_temporal_consistency_weight(
    raw_ndvi: float,
    rolling_median: float | None,
) -> float:
    if rolling_median is None:
        return 0.5
    deviation = get_temporal_deviation()
    diff = abs(raw_ndvi - rolling_median)
    return max(0.0, 1.0 - diff / deviation)


def _compute_confidence(
    engine: str,
    cloud_fraction: float | None,
    valid_pixel_fraction: float | None,
    acquisition_at: Any,
    bucket_date: date,
    raw_ndvi: float,
    rolling_median: float | None,
) -> tuple[float, ConfidenceComponents]:
    components = ConfidenceComponents(
        source_weight=_get_source_weight(engine),
        cloud_weight=_compute_cloud_weight(cloud_fraction),
        valid_pixel_weight=_compute_valid_pixel_weight(valid_pixel_fraction),
        recency_weight=_compute_recency_weight(acquisition_at, bucket_date),
        temporal_consistency_weight=_compute_temporal_consistency_weight(
            raw_ndvi, rolling_median
        ),
    )

    confidence = (
        CONFIDENCE_WEIGHTS["source"] * components.source_weight
        + CONFIDENCE_WEIGHTS["cloud"] * components.cloud_weight
        + CONFIDENCE_WEIGHTS["valid_pixel"] * components.valid_pixel_weight
        + CONFIDENCE_WEIGHTS["recency"] * components.recency_weight
        + CONFIDENCE_WEIGHTS["temporal"]
        * components.temporal_consistency_weight
    )

    return _clamp(confidence), components


def get_prior_v2_values(
    farm_id: int,
    engine: str,
    bucket_date: date,
    *,
    window: int | None = None,
) -> list[float]:
    """Get prior valid V2 selected_ndvi values for rolling median.

    Returns up to `window` most recent valid (non-null) V2 values
    before the given bucket_date, ordered by bucket_date ascending.
    """
    if window is None:
        window = get_rolling_window_size()

    qs = (
        NdviDerivedObservation.objects.filter(
            farm_id=farm_id,
            engine=engine,
            bucket_date__lt=bucket_date,
            is_null=False,
            selected_ndvi__isnull=False,
        )
        .order_by("-bucket_date")
        .values_list("selected_ndvi", flat=True)[:window]
    )

    values = [v for v in qs if v is not None]
    values.reverse()
    return values


def _check_null_conditions(
    valid_pixel_fraction: float | None,
    confidence: float,
    raw_ndvi: float | None,
    acquisition_at: Any,
    engine: str,
    prior_v2_count: int,
    is_outlier: bool,
) -> tuple[bool, str | None]:
    """Check if any null-return condition is met.

    Returns (is_null, null_reason).
    """
    vpf_reject = get_valid_pixel_reject_threshold()
    low_conf = get_low_confidence_threshold()
    min_context = get_min_rolling_context()

    if valid_pixel_fraction is not None and valid_pixel_fraction < vpf_reject:
        return True, "low_valid_pixel_fraction"

    if confidence < low_conf:
        return True, "low_confidence"

    if raw_ndvi is None:
        return True, "missing_ndvi_value"

    if acquisition_at is None:
        return True, "missing_acquisition_time"

    if prior_v2_count < min_context and engine.lower() not in (
        "sentinel-2",
        "sentinelhub",
        "stac",
    ):
        return True, "insufficient_rolling_context"

    if is_outlier:
        return True, "outlier_rejected"

    return False, None


def _check_outlier(
    raw_ndvi: float,
    rolling_median: float | None,
    confidence: float,
    valid_pixel_fraction: float | None,
) -> bool:
    """Check if observation should be rejected as outlier.

    All conditions must be true for outlier rejection.
    """
    if rolling_median is None:
        return False

    outlier_thresh = get_outlier_threshold()
    accept_thresh = get_accept_threshold()
    vpf_thresh = 0.70

    delta = abs(raw_ndvi - rolling_median)
    if delta < outlier_thresh:
        return False

    if confidence >= accept_thresh:
        return False

    if valid_pixel_fraction is None or valid_pixel_fraction >= vpf_thresh:
        return False

    return True


def _compute_smoothed(
    raw_ndvi: float,
    prior_v2_values: list[float],
) -> float | None:
    """Compute smoothed NDVI using median of raw + prior window.

    Returns None if fewer than min_smooth_values exist.
    """
    min_vals = get_min_smooth_values()
    all_values = [raw_ndvi] + prior_v2_values

    if len(all_values) < min_vals:
        return None

    result = _median(all_values)
    return result


def _build_quality_flags(
    v1_flags: dict[str, Any],
    confidence: float,
    is_outlier: bool,
    is_null: bool,
    null_reason: str | None,
) -> dict[str, bool]:
    """Build V2 quality flags from V1 flags and V2 computation results."""
    low_conf = get_low_confidence_threshold()

    flags = {
        "cloud_heavy": bool(v1_flags.get("cloud_heavy", False)),
        "partial_tile": bool(v1_flags.get("partial_tile", False)),
        "low_valid_pixel_fraction": bool(
            v1_flags.get("low_valid_pixel_fraction", False)
        ),
        "low_confidence": confidence < low_conf,
        "outlier_removed": is_outlier,
        "fallback_used": False,
        "source_disagreement": False,
        "s1_context_wet_soil": bool(v1_flags.get("water_detected", False)),
    }

    if is_null and null_reason:
        flags[f"null_{null_reason}"] = True

    return flags


def build_v2_observation(
    v1_observation: NdviObservation,
    *,
    prior_v2_values: list[float] | None = None,
) -> V2Result:
    """Build a V2 observation from a V1 observation.

    This is the core Phase 2 V2 quality engine. It computes:
    - Confidence score with weighted components
    - Rolling median from prior V2 history
    - Outlier detection
    - Temporal smoothing
    - Quality flags
    - Null-return conditions

    Args:
        v1_observation: The persisted V1 raw observation.
        prior_v2_values: Optional pre-fetched prior V2 values.
            If None, fetched from database.

    Returns:
        V2Result with all computed fields.
    """
    engine = v1_observation.engine
    bucket_date = v1_observation.bucket_date
    raw_ndvi = v1_observation.mean
    cloud_fraction = v1_observation.cloud_fraction
    valid_pixel_fraction = v1_observation.valid_pixel_fraction
    acquisition_at = v1_observation.acquired_at
    v1_flags = v1_observation.quality_flags or {}

    if prior_v2_values is None:
        prior_v2_values = get_prior_v2_values(
            v1_observation.farm_id,
            engine,
            bucket_date,
        )

    rolling_median = _median(prior_v2_values) if prior_v2_values else None
    prior_count = len(prior_v2_values)

    max_conf_without_context = get_max_confidence_without_context()
    min_context = get_min_rolling_context()

    confidence, components = _compute_confidence(
        engine=engine,
        cloud_fraction=cloud_fraction,
        valid_pixel_fraction=valid_pixel_fraction,
        acquisition_at=acquisition_at,
        bucket_date=bucket_date,
        raw_ndvi=raw_ndvi,
        rolling_median=rolling_median,
    )

    if prior_count < min_context:
        confidence = min(confidence, max_conf_without_context)

    is_outlier = _check_outlier(
        raw_ndvi=raw_ndvi,
        rolling_median=rolling_median,
        confidence=confidence,
        valid_pixel_fraction=valid_pixel_fraction,
    )

    is_null, null_reason = _check_null_conditions(
        valid_pixel_fraction=valid_pixel_fraction,
        confidence=confidence,
        raw_ndvi=raw_ndvi,
        acquisition_at=acquisition_at,
        engine=engine,
        prior_v2_count=prior_count,
        is_outlier=is_outlier,
    )

    selected_ndvi = None if is_null else raw_ndvi

    smoothed_ndvi = _compute_smoothed(raw_ndvi, prior_v2_values)
    if is_null:
        smoothed_ndvi = None

    quality_flags = _build_quality_flags(
        v1_flags=v1_flags,
        confidence=confidence,
        is_outlier=is_outlier,
        is_null=is_null,
        null_reason=null_reason,
    )

    ndvi_v2_confidence_bucket.labels(engine=engine, source=engine).observe(
        confidence
    )
    ndvi_v2_observation_total.labels(engine=engine, is_null=str(is_null)).inc()
    if is_null:
        ndvi_v2_null_output_total.labels(
            engine=engine, null_reason=null_reason or "unknown"
        ).inc()
    if confidence < get_low_confidence_threshold():
        ndvi_v2_low_confidence_total.labels(engine=engine).inc()

    return V2Result(
        selected_ndvi=selected_ndvi,
        smoothed_ndvi=smoothed_ndvi,
        confidence=confidence,
        confidence_components=components,
        quality_flags=quality_flags,
        is_null=is_null,
        null_reason=null_reason,
    )


def persist_v2_observation(
    v1_observation: NdviObservation,
    v2_result: V2Result,
    *,
    index_type: str = "NDVI",
) -> NdviDerivedObservation:
    """Persist a V2 observation derived from a V1 observation.

    Idempotent on v1_observation_id. Updates existing row if present.

    Args:
        v1_observation: The source V1 observation.
        v2_result: The computed V2 result.

    Returns:
        The persisted NdviDerivedObservation.
    """
    with transaction.atomic():
        obj, _ = NdviDerivedObservation.objects.update_or_create(
            v1_observation=v1_observation,
            defaults={
                "farm": v1_observation.farm,
                "engine": v1_observation.engine,
                "bucket_date": v1_observation.bucket_date,
                "source": v1_observation.engine,
                "index_type": index_type,
                "selected_ndvi": v2_result.selected_ndvi,
                "smoothed_ndvi": v2_result.smoothed_ndvi,
                "confidence": v2_result.confidence,
                "confidence_components": (
                    v2_result.confidence_components.to_dict()
                ),
                "quality_flags": v2_result.quality_flags,
                "is_null": v2_result.is_null,
                "null_reason": v2_result.null_reason,
            },
        )
    return obj


def process_v1_to_v2(
    v1_observation: NdviObservation,
    *,
    persist: bool = True,
) -> tuple[V2Result, NdviDerivedObservation | None]:
    """Full pipeline: build V2 from V1 and optionally persist.

    Args:
        v1_observation: The V1 observation to process.
        persist: Whether to persist the V2 result.

    Returns:
        Tuple of (V2Result, persisted NdviDerivedObservation or None).
    """
    v2_result = build_v2_observation(v1_observation)

    persisted = None
    if persist:
        persisted = persist_v2_observation(v1_observation, v2_result)

    return v2_result, persisted
