"""NDMI-specific quality scorer.

Extends the abstract QualityScorer base with NDMI-specific thresholds,
source weights, confidence formula, and null-return conditions.

Key differences from NDVI/NDWI:
- SWIR band noise detection (SWIR1 is noisier than NIR/Red)
- Moisture range validation: valid NDMI range is [-1, 1], typical [-0.5, 0.8]
- Cloud shadow impact: SWIR1 is sensitive to shadow, reducing confidence
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db import transaction

from ndvi.metrics import (
    ndvi_v2_confidence_bucket,
    ndvi_v2_low_confidence_total,
    ndvi_v2_null_output_total,
    ndvi_v2_observation_total,
)
from ndvi.models import NdviDerivedObservation, NdviObservation
from ndvi.v2_quality import (
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
)
from science.quality.base import (
    QualityResult,
    QualityScorer,
    register_quality_scorer,
)

logger = logging.getLogger(__name__)

# ── NDMI-specific thresholds ───────────────────────────────────────────

NDMI_SOURCE_WEIGHTS: dict[str, float] = {
    "sentinel-2": 1.00,
    "sentinelhub": 1.00,
    "stac": 1.00,
    "landsat": 0.80,
    "modis": 0.60,
}

NDMI_CONFIDENCE_WEIGHTS: dict[str, float] = {
    "source": 0.30,
    "cloud": 0.25,
    "valid_pixel": 0.25,
    "recency": 0.10,
    "temporal": 0.10,
}

# NDMI-specific defaults
NDMI_DEFAULT_OUTLIER_THRESHOLD: float = 0.20
NDMI_DEFAULT_ACCEPT_THRESHOLD: float = 0.70
NDMI_DEFAULT_LOW_CONFIDENCE_THRESHOLD: float = 0.45
NDMI_DEFAULT_VALID_PIXEL_REJECT: float = 0.25
NDMI_DEFAULT_MIN_ROLLING_CONTEXT: int = 3
NDMI_DEFAULT_MAX_CONFIDENCE_WITHOUT_CONTEXT: float = 0.49
NDMI_DEFAULT_SWIR_NOISE_MAX: float = 0.10
"""Maximum acceptable SWIR band noise relative to NIR."""


def _float_setting(name: str, default: float) -> float:
    return float(getattr(settings, name, default))


def _int_setting(name: str, default: int) -> int:
    return int(getattr(settings, name, default))


def _detect_swir_noise(raw_ndmi: float) -> bool:
    """Detect potential SWIR band noise in an NDMI value.

    NDMI values that exceed typical valid range suggest stray light
    or SWIR band noise. Flag values outside [-0.9, 0.9] as noisy.
    """
    return raw_ndmi < -0.9 or raw_ndmi > 0.9


def _detect_cloud_shadow(
    cloud_fraction: float | None,
    valid_pixel_fraction: float | None,
    raw_ndmi: float,
) -> bool:
    """Detect potential cloud shadow impact on NDMI.

    Cloud shadows depress SWIR1 reflectance, causing anomalously high
    NDMI values. Heuristic: if cloud fraction is moderate (5-40%),
    valid pixel fraction is low, and NDMI is unusually high, flag as
    potential shadow.
    """
    if cloud_fraction is None or valid_pixel_fraction is None:
        return False
    return (
        0.05 <= cloud_fraction <= 0.40
        and valid_pixel_fraction < 0.60
        and raw_ndmi > 0.6
    )


def _validate_moisture_range(raw_ndmi: float) -> bool:
    """Validate that NDMI is within a physically plausible range.

    NDMI formula: (NIR - SWIR1) / (NIR + SWIR1)
    Theoretical range: [-1, 1]
    Typical range for vegetation: [-0.5, 0.8]
    Values outside [-0.95, 0.95] are likely invalid.
    """
    return -0.95 <= raw_ndmi <= 0.95


@register_quality_scorer("NDMI")
class NDMIQualityScorer(QualityScorer):
    """NDMI-specific quality scorer.

    Extends the base QualityScorer with:
    - SWIR band noise detection
    - Moisture range validation
    - Cloud shadow impact detection
    - NDMI-appropriate outlier thresholds
    """

    SOURCE_WEIGHTS = NDMI_SOURCE_WEIGHTS
    CONFIDENCE_WEIGHTS = NDMI_CONFIDENCE_WEIGHTS

    def score(
        self,
        observation: NdviObservation,
        *,
        prior_values: list[float] | None = None,
    ) -> QualityResult:
        """Score a single NDMI V1 observation.

        Builds a V2 result by computing confidence components, checking
        null conditions, outlier detection, and NDMI-specific quality
        checks (SWIR noise, moisture range, cloud shadow).

        Args:
            observation: The V1 NdviObservation to score.
            prior_values: Optional pre-fetched prior V2 selected values.

        Returns:
            QualityResult with scored NDMI value, confidence, and flags.
        """
        engine = observation.engine
        bucket_date = observation.bucket_date
        raw_ndmi = observation.mean
        cloud_fraction = observation.cloud_fraction
        valid_pixel_fraction = observation.valid_pixel_fraction
        acquisition_at = observation.acquired_at
        v1_flags = observation.quality_flags or {}

        if prior_values is None:
            prior_values = get_prior_v2_values(
                observation.farm_id,
                engine,
                bucket_date,
            )

        rolling_median = _median(prior_values) if prior_values else None
        prior_count = len(prior_values) if prior_values else 0

        # Compute confidence components
        source_weight = NDMI_SOURCE_WEIGHTS.get(
            engine, _get_source_weight(engine)
        )
        cloud_weight = _compute_cloud_weight(cloud_fraction)
        vpw = _compute_valid_pixel_weight(valid_pixel_fraction)
        recency_weight = _compute_recency_weight(acquisition_at, bucket_date)
        temporal_weight = _compute_temporal_consistency_weight(
            raw_ndmi, rolling_median
        )

        confidence = (
            NDMI_CONFIDENCE_WEIGHTS["source"] * source_weight
            + NDMI_CONFIDENCE_WEIGHTS["cloud"] * cloud_weight
            + NDMI_CONFIDENCE_WEIGHTS["valid_pixel"] * vpw
            + NDMI_CONFIDENCE_WEIGHTS["recency"] * recency_weight
            + NDMI_CONFIDENCE_WEIGHTS["temporal"] * temporal_weight
        )
        confidence = _clamp(confidence)

        max_conf_without_context = _float_setting(
            "NDMI_MAX_CONFIDENCE_WITHOUT_CONTEXT",
            NDMI_DEFAULT_MAX_CONFIDENCE_WITHOUT_CONTEXT,
        )
        min_context = _int_setting(
            "NDMI_MIN_ROLLING_CONTEXT",
            NDMI_DEFAULT_MIN_ROLLING_CONTEXT,
        )
        if prior_count < min_context:
            confidence = min(confidence, max_conf_without_context)

        # NDMI-specific checks (skip if raw_ndmi is None)
        swir_noise = False
        moisture_valid = True
        shadow_impact = False
        if raw_ndmi is not None:
            swir_noise = _detect_swir_noise(raw_ndmi)
            moisture_valid = _validate_moisture_range(raw_ndmi)
            shadow_impact = _detect_cloud_shadow(
                cloud_fraction, valid_pixel_fraction, raw_ndmi
            )

        # Apply confidence penalty for detected issues
        if swir_noise:
            confidence = min(confidence, 0.30)
        if shadow_impact:
            confidence = min(confidence, 0.50)
        if not moisture_valid:
            confidence = 0.0

        outlier_threshold = _float_setting(
            "NDMI_OUTLIER_THRESHOLD", NDMI_DEFAULT_OUTLIER_THRESHOLD
        )
        accept_threshold = _float_setting(
            "NDMI_ACCEPT_THRESHOLD", NDMI_DEFAULT_ACCEPT_THRESHOLD
        )
        vpf_threshold = _float_setting(
            "NDMI_VALID_PIXEL_REJECT", NDMI_DEFAULT_VALID_PIXEL_REJECT
        )

        is_outlier = _check_outlier(
            raw_ndvi=raw_ndmi,
            rolling_median=rolling_median,
            confidence=confidence,
            valid_pixel_fraction=valid_pixel_fraction,
            outlier_threshold=outlier_threshold,
            accept_threshold=accept_threshold,
            vpf_threshold=vpf_threshold,
        )

        low_confidence_threshold = _float_setting(
            "NDMI_LOW_CONFIDENCE_THRESHOLD",
            NDMI_DEFAULT_LOW_CONFIDENCE_THRESHOLD,
        )
        valid_pixel_reject = _float_setting(
            "NDMI_VALID_PIXEL_REJECT", NDMI_DEFAULT_VALID_PIXEL_REJECT
        )

        is_null, null_reason = _check_null_conditions(
            valid_pixel_fraction=valid_pixel_fraction,
            confidence=confidence,
            raw_ndvi=raw_ndmi,
            acquisition_at=acquisition_at,
            engine=engine,
            prior_v2_count=prior_count,
            is_outlier=is_outlier,
            vpf_reject_threshold=valid_pixel_reject,
            low_confidence_threshold=low_confidence_threshold,
            min_rolling_context=NDMI_DEFAULT_MIN_ROLLING_CONTEXT,
        )

        selected_ndmi = None if is_null else raw_ndmi
        smoothed_ndmi = _compute_smoothed(raw_ndmi, prior_values)
        if is_null:
            smoothed_ndmi = None

        # Inject NDMI-specific flags
        quality_flags = _build_quality_flags(
            v1_flags=v1_flags,
            confidence=confidence,
            is_outlier=is_outlier,
            is_null=is_null,
            null_reason=null_reason,
        )
        if swir_noise:
            quality_flags["swir_noise"] = True
        if shadow_impact:
            quality_flags["cloud_shadow"] = True
        if not moisture_valid:
            quality_flags["invalid_moisture_range"] = True

        confidence_components = {
            "source_weight": round(source_weight, 4),
            "cloud_weight": round(cloud_weight, 4),
            "valid_pixel_weight": round(vpw, 4),
            "recency_weight": round(recency_weight, 4),
            "temporal_consistency_weight": round(temporal_weight, 4),
        }

        # Emit metrics
        ndvi_v2_confidence_bucket.labels(engine=engine, source=engine).observe(
            confidence
        )
        ndvi_v2_observation_total.labels(
            engine=engine, is_null=str(is_null)
        ).inc()
        if is_null:
            ndvi_v2_null_output_total.labels(
                engine=engine, null_reason=null_reason or "unknown"
            ).inc()
        if confidence < low_confidence_threshold:
            ndvi_v2_low_confidence_total.labels(engine=engine).inc()

        result = QualityResult(
            selected_value=selected_ndmi,
            smoothed_value=smoothed_ndmi,
            confidence=confidence,
            confidence_components=confidence_components,
            quality_flags=quality_flags,
            is_null=is_null,
            null_reason=null_reason,
        )
        return result

    def persist(
        self,
        observation: NdviObservation,
        result: QualityResult,
        index_type: str = "NDMI",
    ) -> NdviDerivedObservation:
        """Persist a scored NDMI quality result.

        Args:
            observation: The source V1 observation.
            result: The computed QualityResult.
            index_type: Index type label (default "NDMI").

        Returns:
            The persisted NdviDerivedObservation.
        """
        with transaction.atomic():
            obj, _ = NdviDerivedObservation.objects.update_or_create(
                v1_observation=observation,
                defaults={
                    "farm": observation.farm,
                    "engine": observation.engine,
                    "bucket_date": observation.bucket_date,
                    "source": observation.engine,
                    "index_type": index_type,
                    "selected_ndvi": result.selected_value,
                    "smoothed_ndvi": result.smoothed_value,
                    "confidence": result.confidence,
                    "confidence_components": result.confidence_components,
                    "quality_flags": result.quality_flags,
                    "is_null": result.is_null,
                    "null_reason": result.null_reason,
                },
            )
        return obj


# ── Functional API (for tasks.py compatibility) ────────────────────────


def build_ndmi_v2_observation(
    v1_observation: NdviObservation,
    *,
    prior_v2_values: list[float] | None = None,
) -> QualityResult:
    """Build a V2 observation from a V1 observation using NDMIQualityScorer.

    Functional convenience wrapper around the class-based scorer.
    Follows the same pattern as `build_ndwi_v2_observation` in quality_ndwi.py.

    Args:
        v1_observation: The V1 observation to process.
        prior_v2_values: Optional pre-fetched prior V2 values.

    Returns:
        QualityResult with scored NDMI value.
    """
    scorer = NDMIQualityScorer()
    return scorer.score(v1_observation, prior_values=prior_v2_values)


def process_ndmi_v1_to_v2(
    v1_observation: NdviObservation,
    *,
    persist: bool = True,
) -> tuple[QualityResult, NdviDerivedObservation | None]:
    """Full NDMI V2 pipeline: build V2 from V1 and optionally persist.

    Follows the same pattern as `process_ndwi_v1_to_v2` in quality_ndwi.py.

    Args:
        v1_observation: The V1 observation to process.
        persist: Whether to persist the V2 result.

    Returns:
        Tuple of (QualityResult, persisted NdviDerivedObservation or None).
    """
    return NDMIQualityScorer.process_v1_to_v2(
        v1_observation, persist=persist, index_type="NDMI"
    )
