"""NDMI-specific fusion engine.

NDMI fusion uses the same candidate-gathering and selection logic as
the NDVI fusion engine, but applies NDMI-specific confidence values
from the NDMI quality scorer and NDMI-specific thresholds.

Architecture: delegates to the shared fusion utilities from ndvi.fusion
for decision-tree selection while using NDMI-specific gathering.
"""

from __future__ import annotations

import logging
from datetime import date

from django.conf import settings

from ndvi.fusion import (
    _apply_confidence_degradation,
    _select_by_decision_tree,
)
from ndvi.models import NdviObservation
from science.fusion.base import (
    FusionCandidate,
    FusionEngine,
    FusionResult,
    register_fusion_engine,
)
from science.quality.ndmi import build_ndmi_v2_observation

logger = logging.getLogger(__name__)

NDMI_MOISTURE_THRESHOLD: float = 0.20
"""Threshold above which NDMI indicates high moisture content."""

NDMI_STRESS_THRESHOLD: float = -0.10
"""Threshold below which NDMI indicates moisture stress."""


def classify_ndmi(value: float) -> str:
    """Classify an NDMI value into a moisture class label.

    Thresholds are configurable via settings:
        NDMI_MOISTURE_THRESHOLD (default 0.20)
        NDMI_STRESS_THRESHOLD (default -0.10)

    Returns one of:
        "high_moisture"  — value >= moisture_threshold
        "adequate"       — stress_threshold <= value < moisture_threshold
        "moisture_stress" — value < stress_threshold
    """
    moisture_threshold = float(
        getattr(settings, "NDMI_MOISTURE_THRESHOLD", NDMI_MOISTURE_THRESHOLD)
    )
    stress_threshold = float(
        getattr(settings, "NDMI_STRESS_THRESHOLD", NDMI_STRESS_THRESHOLD)
    )
    if value >= moisture_threshold:
        return "high_moisture"
    if value >= stress_threshold:
        return "adequate"
    return "moisture_stress"


@register_fusion_engine("NDMI")
class NDMIFusionEngine(FusionEngine):
    """NDMI-specific fusion engine.

    Gathers NDMI candidates from all available engines, scores each
    with the NDMI quality scorer, and selects the best candidate
    using the standard fusion decision tree.
    """

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

    def gather_candidates(
        self,
        farm_id: int,
        bucket_date: date,
        index_type: str = "NDMI",
    ) -> list[FusionCandidate]:
        """Gather NDMI candidates for a (farm, bucket_date).

        Fetches V1 NDMI observations and scores each with the NDMI quality
        scorer. Discards candidates that fail null-return or confidence
        screening.

        Args:
            farm_id: The farm to gather candidates for.
            bucket_date: The date bucket to gather candidates for.
            index_type: Must be "NDMI".

        Returns:
            List of FusionCandidate objects that passed screening.
        """
        v1_observations = list(
            NdviObservation.objects.filter(
                farm_id=farm_id,
                bucket_date=bucket_date,
                index_type=index_type,
                is_latest=True,
            )
        )

        candidates: list[FusionCandidate] = []
        low_conf_threshold = float(
            getattr(settings, "NDMI_LOW_CONFIDENCE_THRESHOLD", 0.45)
        )

        for v1 in v1_observations:
            v2_result = build_ndmi_v2_observation(v1)
            if v2_result.is_null:
                continue
            if v2_result.confidence < low_conf_threshold:
                continue
            degraded = _apply_confidence_degradation(
                v2_result.confidence, v1.engine
            )
            candidates.append(
                FusionCandidate(
                    source=v1.engine,
                    bucket_date=v1.bucket_date,
                    selected_value=v2_result.selected_value,
                    confidence=v2_result.confidence,
                    observation=v1,
                    degraded_confidence=degraded,
                )
            )

        return candidates

    def select_candidate(
        self,
        candidates: list[FusionCandidate],
    ) -> FusionResult:
        """Select the best NDMI candidate using the fusion decision tree.

        Converts FusionCandidate objects to the format expected by
        `_select_by_decision_tree`, runs selection, and classifies the
        result into a moisture class.

        Args:
            candidates: List of NDMI FusionCandidates.

        Returns:
            FusionResult with selected candidate or None.
        """
        from ndvi.fusion import FusionCandidate as NdviFusionCandidate
        from ndvi.v2_quality import V2Result

        if not candidates:
            return FusionResult(
                selected=None,
                candidates_evaluated=0,
                candidates_discarded=0,
                decision_reason="No NDMI candidates passed quality screening",
            )

        # Convert to ndvi.fusion format for decision tree
        ndvi_candidates = []
        for c in candidates:
            from ndvi.v2_quality import ConfidenceComponents

            v2_result = V2Result(
                selected_ndvi=c.selected_value,
                smoothed_ndvi=c.selected_value,
                confidence=c.confidence,
                confidence_components=ConfidenceComponents(),
                quality_flags={},
                is_null=c.selected_value is None,
                null_reason=None,
            )
            # We only need v1_observation for .engine in the decision tree
            from ndvi.models import NdviObservation

            v1 = c.observation or NdviObservation()
            v1.engine = c.source
            fc = NdviFusionCandidate(
                v1_observation=v1,
                v2_result=v2_result,
                degraded_confidence=c.degraded_confidence,
            )
            ndvi_candidates.append(fc)

        result = _select_by_decision_tree(ndvi_candidates)

        # Convert back to science/fusion format
        selected = None
        if result.selected is not None:
            matched = None
            for c in candidates:
                if (
                    c.source == result.selected.source
                    and c.selected_value == result.selected.selected_ndvi
                ):
                    matched = c
                    break
            if matched is None:
                # Fallback: use source match
                for c in candidates:
                    if c.source == result.selected.source:
                        matched = c
                        break
            if matched is not None:
                matched.is_selected = True
                matched.selection_reason = result.selected.selection_reason
                selected = matched

        # Classify NDMI value into moisture class
        water_class = None
        if selected is not None and selected.selected_value is not None:
            water_class = classify_ndmi(selected.selected_value)

        return FusionResult(
            selected=selected,
            candidates_evaluated=result.candidates_evaluated,
            candidates_discarded=result.candidates_discarded,
            decision_reason=result.decision_reason,
            conflict_detected=result.conflict_detected,
            quality_flags=result.quality_flags,
            water_class=water_class,
        )


# ── Functional API (for tasks.py compatibility) ────────────────────────


def run_ndmi_fusion(
    farm_id: int,
    bucket_date: date,
) -> FusionResult:
    """Run NDMI fusion for a (farm, bucket_date).

    Convenience function that delegates to NDMIFusionEngine.

    Args:
        farm_id: Farm to fuse for.
        bucket_date: Date bucket to fuse.

    Returns:
        FusionResult with the selected candidate.
    """
    engine = NDMIFusionEngine()
    return engine.fuse(farm_id, bucket_date, index_type="NDMI")
