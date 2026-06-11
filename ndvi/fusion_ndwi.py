"""NDWI-specific fusion service.

NDWI fusion uses the same candidate-gathering and selection logic as
the NDVI fusion engine, but applies NDWI-specific confidence values
from the NDWI quality engine.

Architecture: delegates to shared fusion utilities for decision-tree
selection while using NDWI-specific candidate gathering and scoring.
"""

from __future__ import annotations

import logging
from datetime import date

from django.conf import settings

from ndvi.fusion import (
    FusionCandidate,
    FusionResult,
    _apply_confidence_degradation,
    _select_by_decision_tree,
)
from ndvi.models import NdviObservation
from ndvi.quality_ndwi import build_ndwi_v2_observation

logger = logging.getLogger(__name__)


def run_ndwi_fusion(
    farm_id: int,
    bucket_date: date,
) -> FusionResult:
    """Run NDWI fusion for a (farm, bucket_date).

    Gathers candidates from all available engines, scores each with
    the NDWI quality engine, and selects the best candidate using
    the standard fusion decision tree.

    Args:
        farm_id: Farm to fuse for.
        bucket_date: Date bucket to fuse.

    Returns:
        FusionResult with the selected candidate.
    """
    v1_observations = list(
        NdviObservation.objects.filter(
            farm_id=farm_id,
            bucket_date=bucket_date,
            index_type="NDWI",
            is_latest=True,
        )
    )

    candidates: list[FusionCandidate] = []
    low_conf_threshold = float(
        getattr(settings, "NDWI_LOW_CONFIDENCE_THRESHOLD", 0.45)
    )

    for v1 in v1_observations:
        v2_result = build_ndwi_v2_observation(v1)
        if v2_result.is_null:
            continue
        if v2_result.confidence < low_conf_threshold:
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

    if not candidates:
        return FusionResult(
            selected=None,
            candidates_evaluated=len(v1_observations),
            candidates_discarded=len(v1_observations),
            decision_reason="No NDWI candidates passed quality screening",
        )

    return _select_by_decision_tree(candidates)
