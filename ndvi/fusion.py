"""Phase 3-4 Fusion service: fallback selector and intelligence layer.

Phase 3 (Multi-Engine Fallback):
  Gathers candidate V2 observations for a (farm, bucket_date), scores them
  through the V2 quality engine, and selects the best candidate using a
  deterministic decision tree with confidence degradation on fallback.

Phase 4 (Fusion and Intelligence):
  Adds cross-source disagreement detection, Sentinel-1 context for anomaly
  explanation, and rule-based fusion. Sentinel-1 only affects context and
  flags, never NDVI selection.

Architecture spec: docs/architecture/ndvi-system-evolution-phased-spec.md
  Section 8 (Phase 3 - Multi-Engine Fallback)
  Section 9 (Phase 4 - Fusion and Intelligence).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from ndvi.metrics import (
    ndvi_fallback_usage_total,
    ndvi_source_disagreement_total,
    ndvi_v2_suppressed_observations_total,
)
from ndvi.models import NdviDerivedObservation, NdviObservation
from ndvi.sentinel1_context import (
    Sentinel1Context,
    detect_anomaly,
    fetch_sentinel1_context,
    merge_s1_context_flags,
)
from ndvi.v2_quality import (
    V2Result,
    build_v2_observation,
    get_low_confidence_threshold,
)

logger = logging.getLogger(__name__)

SOURCE_PRIORITY = ["sentinel-2", "sentinelhub", "stac", "landsat", "modis"]

CONFIDENCE_DEGRADATION = {
    "sentinel-2": 1.00,
    "sentinelhub": 1.00,
    "stac": 1.00,
    "landsat": 0.90,
    "modis": 0.80,
}

SOURCE_CONFIDENCE_THRESHOLDS = {
    "sentinel-2": 0.75,
    "sentinelhub": 0.75,
    "stac": 0.75,
    "landsat": 0.70,
    "modis": 0.60,
}

NDVI_CONFLICT_THRESHOLD = 0.10
NDVI_CONFLICT_CONFIDENCE_CAP = 0.75


def _normalize_source(source: str) -> str:
    return source.lower().strip()


def _get_source_priority_index(source: str) -> int:
    normalized = _normalize_source(source)
    for i, candidate in enumerate(SOURCE_PRIORITY):
        if normalized == candidate:
            return i
    return len(SOURCE_PRIORITY)


def _is_primary_source(source: str) -> bool:
    normalized = _normalize_source(source)
    return normalized in ("sentinel-2", "sentinelhub", "stac")


@dataclass
class FusionCandidate:
    """A candidate V2 observation for fusion selection."""

    v1_observation: NdviObservation
    v2_result: V2Result
    derived: NdviDerivedObservation | None = None
    degraded_confidence: float = 0.0
    is_selected: bool = False
    selection_reason: str | None = None

    @property
    def source(self) -> str:
        return self.v1_observation.engine

    @property
    def bucket_date(self) -> date:
        return self.v1_observation.bucket_date

    @property
    def confidence(self) -> float:
        return self.degraded_confidence

    @property
    def selected_ndvi(self) -> float | None:
        return self.v2_result.selected_ndvi


@dataclass
class FusionResult:
    """Output of the fusion service.

    quality_flags are populated during selection and include:
      - source_disagreement: set when conflict detected
      - fallback_used: set when Landsat or MODIS selected
      - s1_wet_soil / s1_flooding / etc.: from Sentinel-1 context
      - anomaly flags when S1 context explains NDVI anomaly

    ndwi_water_class is set post-fusion for NDWI endpoints:
      - "open_water", "wet_soil", "dry_soil", "vegetation_dominated"
      - Only populated when index_type=NDWI and a candidate is selected.
    """

    selected: FusionCandidate | None
    candidates_evaluated: int
    candidates_discarded: int
    decision_reason: str
    conflict_detected: bool = False
    quality_flags: dict[str, bool] = field(default_factory=dict)
    ndwi_water_class: str | None = None


def _apply_confidence_degradation(
    confidence: float,
    source: str,
) -> float:
    multiplier = CONFIDENCE_DEGRADATION.get(_normalize_source(source), 1.00)
    return round(confidence * multiplier, 4)


def _get_source_threshold(source: str) -> float:
    return SOURCE_CONFIDENCE_THRESHOLDS.get(_normalize_source(source), 0.50)


def gather_candidates(
    farm_id: int,
    bucket_date: date,
) -> list[FusionCandidate]:
    """Gather all V2 candidates for a (farm, bucket_date).

    Fetches V1 observations and builds V2 results for each.
    Discards candidates that fail null-return conditions.

    Args:
        farm_id: The farm to gather candidates for.
        bucket_date: The date bucket to gather candidates for.

    Returns:
        List of FusionCandidate objects that passed initial screening.
    """
    v1_observations = list(
        NdviObservation.objects.filter(
            farm_id=farm_id,
            bucket_date=bucket_date,
            is_latest=True,
        )
    )

    candidates: list[FusionCandidate] = []
    low_conf_threshold = get_low_confidence_threshold()

    for v1 in v1_observations:
        v2_result = build_v2_observation(v1)

        if v2_result.is_null:
            logger.debug(
                "fusion.candidate_discarded farm=%s date=%s engine=%s "
                "reason=%s",
                farm_id,
                bucket_date,
                v1.engine,
                v2_result.null_reason,
            )
            continue

        if v2_result.confidence < low_conf_threshold:
            logger.debug(
                "fusion.candidate_low_confidence farm=%s date=%s "
                "engine=%s confidence=%.4f",
                farm_id,
                bucket_date,
                v1.engine,
                v2_result.confidence,
            )
            continue

        degraded = _apply_confidence_degradation(
            v2_result.confidence, v1.engine
        )

        candidates.append(
            FusionCandidate(
                v1_observation=v1,
                v2_result=v2_result,
                degraded_confidence=degraded,
            )
        )

    return candidates


def _check_conflict(
    candidates: list[FusionCandidate],
) -> tuple[bool, str]:
    """Check if top candidates conflict beyond the disagreement threshold.

    If the top two surviving candidates differ by >= 0.10 NDVI and
    neither exceeds 0.75 confidence, return NULL.

    Args:
        candidates: Sorted list of surviving candidates.

    Returns:
        (conflict_detected, reason).
    """
    if len(candidates) < 2:
        return False, ""

    top = candidates[0]
    second = candidates[1]

    if top.selected_ndvi is None or second.selected_ndvi is None:
        return False, ""

    ndvi_diff = abs(top.selected_ndvi - second.selected_ndvi)
    if ndvi_diff < NDVI_CONFLICT_THRESHOLD:
        return False, ""

    if (
        top.confidence >= NDVI_CONFLICT_CONFIDENCE_CAP
        or second.confidence >= NDVI_CONFLICT_CONFIDENCE_CAP
    ):
        return False, ""

    return True, (
        f"source_disagreement: diff={ndvi_diff:.4f}, "
        f"top_conf={top.confidence:.4f}, "
        f"second_conf={second.confidence:.4f}"
    )


def _build_result_flags(
    selected: FusionCandidate | None,
    conflict_detected: bool,
    s1_context: Sentinel1Context | None = None,
) -> dict[str, bool]:
    """Build quality flags for a fusion result.

    Args:
        selected: The selected candidate (may be None).
        conflict_detected: Whether a source conflict was detected.
        s1_context: Optional Sentinel-1 context for anomaly detection.

    Returns:
        Dict of quality flags.
    """
    flags: dict[str, bool] = {
        "source_disagreement": conflict_detected,
        "fallback_used": False,
        "anomaly_detected": False,
    }

    if selected is not None and not conflict_detected:
        norm_source = _normalize_source(selected.source)
        if norm_source in ("landsat", "modis"):
            flags["fallback_used"] = True

        s1 = s1_context or Sentinel1Context()
        flags = merge_s1_context_flags(flags, s1)

        if s1.has_any_signal() and selected.selected_ndvi is not None:
            is_anom, anom_reason = detect_anomaly(selected.selected_ndvi, s1)
            if is_anom and anom_reason:
                flags["anomaly_detected"] = True
                flags[f"anomaly_{anom_reason}"] = True

    return flags


def _select_by_decision_tree(
    candidates: list[FusionCandidate],
    s1_context: Sentinel1Context | None = None,
) -> FusionResult:
    """Apply the deterministic decision tree to select the best candidate.

    Decision tree (from architecture spec):
    1. If one Sentinel-2 candidate and confidence >= 0.75 -> select it
    2. Else if one Landsat candidate and confidence >= 0.70 -> select it
    3. Else if one MODIS candidate and confidence >= 0.60 -> select it
    4. Else select highest confidence remaining
    5. Tie-break by source priority
    6. No survivor -> NULL

    Phase 4 additions:
    - Sets source_disagreement flag when conflict detected
    - Sets fallback_used flag when Landsat or MODIS selected
    - Merges Sentinel-1 context flags
    - Detects anomalies using Sentinel-1 context

    Args:
        candidates: List of candidates that passed initial screening.
        s1_context: Optional Sentinel-1 context for flag building.

    Returns:
        FusionResult with selected candidate or None.
    """
    if not candidates:
        return FusionResult(
            selected=None,
            candidates_evaluated=0,
            candidates_discarded=0,
            decision_reason="no_candidates",
            quality_flags=_build_result_flags(None, False, s1_context),
        )

    conflict_detected, conflict_reason = _check_conflict(candidates)
    if conflict_detected:
        sources = [c.source for c in candidates[:2]]
        kw: dict[str, str] = {}
        if len(sources) >= 2:
            kw = {"engine_a": sources[0], "engine_b": sources[1]}
        ndvi_source_disagreement_total.labels(**kw).inc()
        return FusionResult(
            selected=None,
            candidates_evaluated=len(candidates),
            candidates_discarded=len(candidates),
            decision_reason=conflict_reason,
            conflict_detected=True,
            quality_flags=_build_result_flags(None, True, s1_context),
        )

    primary_candidates = [
        c for c in candidates if _is_primary_source(c.source)
    ]
    landsat_candidates = [
        c for c in candidates if _normalize_source(c.source) == "landsat"
    ]
    modis_candidates = [
        c for c in candidates if _normalize_source(c.source) == "modis"
    ]

    s2_threshold = _get_source_threshold("sentinel-2")
    s2_qualified = [
        c for c in primary_candidates if c.confidence >= s2_threshold
    ]
    if len(s2_qualified) == 1:
        selected = s2_qualified[0]
        selected.is_selected = True
        selected.selection_reason = "sentinel2_qualified"
        return FusionResult(
            selected=selected,
            candidates_evaluated=len(candidates),
            candidates_discarded=len(candidates) - 1,
            decision_reason="sentinel2_selected",
            quality_flags=_build_result_flags(selected, False, s1_context),
        )

    discarded = len(candidates) - len(primary_candidates)
    if discarded > 0:
        ndvi_v2_suppressed_observations_total.labels(
            reason="passed_over_primary"
        ).inc(discarded)

    ls_threshold = _get_source_threshold("landsat")
    ls_qualified = [
        c for c in landsat_candidates if c.confidence >= ls_threshold
    ]
    if len(ls_qualified) == 1:
        selected = ls_qualified[0]
        selected.is_selected = True
        selected.selection_reason = "landsat_qualified"
        ndvi_fallback_usage_total.labels(
            engine_selected="landsat", engine_primary="sentinel-2"
        ).inc()
        discarded = len(candidates) - 1
        ndvi_v2_suppressed_observations_total.labels(
            reason="passed_over_fallback"
        ).inc(discarded)
        return FusionResult(
            selected=selected,
            candidates_evaluated=len(candidates),
            candidates_discarded=discarded,
            decision_reason="landsat_selected",
            quality_flags=_build_result_flags(selected, False, s1_context),
        )

    modis_threshold = _get_source_threshold("modis")
    modis_qualified = [
        c for c in modis_candidates if c.confidence >= modis_threshold
    ]
    if len(modis_qualified) == 1:
        selected = modis_qualified[0]
        selected.is_selected = True
        selected.selection_reason = "modis_qualified"
        ndvi_fallback_usage_total.labels(
            engine_selected="modis", engine_primary="sentinel-2"
        ).inc()
        discarded = len(candidates) - 1
        ndvi_v2_suppressed_observations_total.labels(
            reason="passed_over_fallback"
        ).inc(discarded)
        return FusionResult(
            selected=selected,
            candidates_evaluated=len(candidates),
            candidates_discarded=discarded,
            decision_reason="modis_selected",
            quality_flags=_build_result_flags(selected, False, s1_context),
        )

    candidates.sort(
        key=lambda c: (
            c.confidence,
            -_get_source_priority_index(c.source),
        )
    )
    candidates.reverse()

    selected = candidates[0]
    selected.is_selected = True
    selected.selection_reason = "highest_confidence"

    if (
        len(candidates) > 1
        and candidates[0].confidence == candidates[1].confidence
    ):
        selected.selection_reason = "tiebreak_source_priority"

    discarded = len(candidates) - 1
    if discarded > 0:
        ndvi_v2_suppressed_observations_total.labels(
            reason="passed_over_confidence"
        ).inc(discarded)

    return FusionResult(
        selected=selected,
        candidates_evaluated=len(candidates),
        candidates_discarded=discarded,
        decision_reason=selected.selection_reason,
        quality_flags=_build_result_flags(selected, False, s1_context),
    )


def fuse_observations(
    farm_id: int,
    bucket_date: date,
    *,
    candidates: list[FusionCandidate] | None = None,
    s1_context: Sentinel1Context | None = None,
) -> FusionResult:
    """Full fusion pipeline: gather, score, select, enhance.

    Phase 3: gather candidates and apply decision tree.
    Phase 4: adds quality flags, Sentinel-1 context, anomaly detection.

    Args:
        farm_id: The farm to fuse observations for.
        bucket_date: The date bucket to fuse observations for.
        candidates: Optional pre-gathered candidates. If None, fetched.
        s1_context: Optional Sentinel-1 context for flag building.
            If None, fetched from fetch_sentinel1_context().

    Returns:
        FusionResult with selected candidate or None.
    """
    if candidates is None:
        candidates = gather_candidates(farm_id, bucket_date)

    if s1_context is None:
        s1_context = fetch_sentinel1_context(farm_id, bucket_date)

    return _select_by_decision_tree(candidates, s1_context=s1_context)
