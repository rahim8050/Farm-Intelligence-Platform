"""NDWI-specific quality engine.

NDWI quality uses the same component formula structure as the NDVI V2
quality engine but with NDWI-appropriate thresholds and source weights.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings

from ndvi.metrics import (
    ndvi_v2_confidence_bucket,
    ndvi_v2_low_confidence_total,
    ndvi_v2_null_output_total,
    ndvi_v2_observation_total,
)
from ndvi.models import NdviDerivedObservation, NdviObservation
from ndvi.v2_quality import (
    ConfidenceComponents,
    V2Result,
    _build_quality_flags,
    _check_null_conditions,
    _check_outlier,
    _clamp,
    _compute_cloud_weight,
    _compute_recency_weight,
    _compute_smoothed,
    _compute_temporal_consistency_weight,
    _compute_valid_pixel_weight,
    _get_source_weight,
    _median,
    get_prior_v2_values,
    persist_v2_observation,
)

NDWI_SOURCE_WEIGHTS: dict[str, float] = {
    "sentinel-2": 1.00,
    "sentinelhub": 1.00,
    "stac": 1.00,
    "landsat": 0.80,
}

NDWI_CONFIDENCE_WEIGHTS = {
    "source": 0.30,
    "cloud": 0.25,
    "valid_pixel": 0.25,
    "recency": 0.10,
    "temporal": 0.10,
}


def _float_setting(name: str, default: float) -> float:
    return float(getattr(settings, name, default))


def _compute_ndwi_confidence(
    engine: str,
    cloud_fraction: float | None,
    valid_pixel_fraction: float | None,
    acquisition_at: Any,
    bucket_date: Any,
    raw_ndvi: float,
    rolling_median: float | None,
) -> tuple[float, ConfidenceComponents]:
    source_weight = NDWI_SOURCE_WEIGHTS.get(engine, _get_source_weight(engine))
    components = ConfidenceComponents(
        source_weight=source_weight,
        cloud_weight=_compute_cloud_weight(cloud_fraction),
        valid_pixel_weight=_compute_valid_pixel_weight(valid_pixel_fraction),
        recency_weight=_compute_recency_weight(acquisition_at, bucket_date),
        temporal_consistency_weight=_compute_temporal_consistency_weight(
            raw_ndvi, rolling_median
        ),
    )
    confidence = (
        NDWI_CONFIDENCE_WEIGHTS["source"] * components.source_weight
        + NDWI_CONFIDENCE_WEIGHTS["cloud"] * components.cloud_weight
        + NDWI_CONFIDENCE_WEIGHTS["valid_pixel"]
        * components.valid_pixel_weight
        + NDWI_CONFIDENCE_WEIGHTS["recency"] * components.recency_weight
        + NDWI_CONFIDENCE_WEIGHTS["temporal"]
        * components.temporal_consistency_weight
    )
    return _clamp(confidence), components


def build_ndwi_v2_observation(
    v1_observation: NdviObservation,
    *,
    prior_v2_values: list[float] | None = None,
) -> V2Result:
    """Build a V2 observation from a V1 observation (NDWI variant).

    Uses NDWI-specific thresholds for confidence, outlier detection,
    and null conditions. The component formula structure is identical
    to the NDVI V2 quality engine.
    """
    engine = v1_observation.engine
    bucket_date = v1_observation.bucket_date
    raw_ndwi = v1_observation.mean
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
    prior_count = len(prior_v2_values) if prior_v2_values else 0

    confidence, components = _compute_ndwi_confidence(
        engine=engine,
        cloud_fraction=cloud_fraction,
        valid_pixel_fraction=valid_pixel_fraction,
        acquisition_at=acquisition_at,
        bucket_date=bucket_date,
        raw_ndvi=raw_ndwi,
        rolling_median=rolling_median,
    )

    max_conf_without_context = _float_setting(
        "NDWI_MAX_CONFIDENCE_WITHOUT_CONTEXT", 0.49
    )
    min_context = int(getattr(settings, "NDWI_MIN_ROLLING_CONTEXT", 3))
    if prior_count < min_context:
        confidence = min(confidence, max_conf_without_context)

    is_outlier = _check_outlier(
        raw_ndvi=raw_ndwi,
        rolling_median=rolling_median,
        confidence=confidence,
        valid_pixel_fraction=valid_pixel_fraction,
        outlier_threshold=0.25,
        accept_threshold=0.70,
        vpf_threshold=0.60,
    )

    is_null, null_reason = _check_null_conditions(
        valid_pixel_fraction=valid_pixel_fraction,
        confidence=confidence,
        raw_ndvi=raw_ndwi,
        acquisition_at=acquisition_at,
        engine=engine,
        prior_v2_count=prior_count,
        is_outlier=is_outlier,
        vpf_reject_threshold=0.25,
        low_confidence_threshold=0.45,
        min_rolling_context=4,
    )

    selected_ndwi = None if is_null else raw_ndwi

    smoothed_ndwi = _compute_smoothed(raw_ndwi, prior_v2_values)
    if is_null:
        smoothed_ndwi = None

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
    low_confidence = _float_setting("NDWI_LOW_CONFIDENCE_THRESHOLD", 0.45)
    if confidence < low_confidence:
        ndvi_v2_low_confidence_total.labels(engine=engine).inc()

    return V2Result(
        selected_ndvi=selected_ndwi,
        smoothed_ndvi=smoothed_ndwi,
        confidence=confidence,
        confidence_components=components,
        quality_flags=quality_flags,
        is_null=is_null,
        null_reason=null_reason,
    )


def process_ndwi_v1_to_v2(
    v1_observation: NdviObservation,
    *,
    persist: bool = True,
) -> tuple[V2Result, NdviDerivedObservation | None]:
    """Full NDWI V2 pipeline: build V2 from V1 and optionally persist.

    Args:
        v1_observation: The V1 observation to process.
        persist: Whether to persist the V2 result.

    Returns:
        Tuple of (V2Result, persisted NdviDerivedObservation or None).
    """
    v2_result = build_ndwi_v2_observation(v1_observation)

    persisted = None
    if persist:
        persisted = persist_v2_observation(
            v1_observation,
            v2_result,
            index_type="NDWI",
        )

    return v2_result, persisted
