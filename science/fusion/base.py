"""Abstract fusion engine base class.

This module defines the base FusionEngine interface that all index-specific
fusion engines must implement. Each engine merges observations from multiple
sources for a given (farm, bucket_date), selects the best candidate, and
produces a fused result with quality flags.

Auth: No authentication — called from Celery tasks.
Response: FusionResult dataclass (not wrapped in API envelope).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class FusionCandidate:
    """A candidate observation for fusion selection.

    Attributes:
        source: Engine/provider name (e.g. "sentinel-2", "landsat").
        bucket_date: The date bucket this candidate belongs to.
        selected_value: The scored index value (may be None if nulled).
        confidence: Confidence score for this candidate.
        observation: The source V1 observation (for provenance).
        degraded_confidence: Confidence after source-priority degradation.
        is_selected: Whether this candidate was ultimately selected.
        selection_reason: Why this candidate was or was not selected.
    """

    source: str
    bucket_date: date
    selected_value: float | None
    confidence: float
    observation: Any = None
    degraded_confidence: float = 0.0
    is_selected: bool = False
    selection_reason: str | None = None


@dataclass
class FusionResult:
    """Output of the fusion engine.

    Attributes:
        selected: The selected FusionCandidate (None if no survivor).
        candidates_evaluated: Total candidates considered.
        candidates_discarded: Candidates discarded during selection.
        decision_reason: Human-readable description of the decision.
        conflict_detected: Whether source disagreement was detected.
        quality_flags: Dict of boolean quality indicators.
        water_class: Optional water/vegetation class label (for NDWI/NDMI).
    """

    selected: FusionCandidate | None = None
    candidates_evaluated: int = 0
    candidates_discarded: int = 0
    decision_reason: str = ""
    conflict_detected: bool = False
    quality_flags: dict[str, bool] = field(default_factory=dict)
    water_class: str | None = None


class FusionEngine(ABC):
    """Abstract base class for index-specific fusion engines.

    Subclasses define how candidates are gathered, scored, and selected
    for a specific spectral index.
    """

    # Source priority order (highest first)
    SOURCE_PRIORITY: list[str] = []

    # Confidence degradation multipliers per source
    CONFIDENCE_DEGRADATION: dict[str, float] = {}

    # Minimum confidence thresholds per source for automatic selection
    SOURCE_CONFIDENCE_THRESHOLDS: dict[str, float] = {}

    def gather_candidates(
        self,
        farm_id: int,
        bucket_date: date,
        index_type: str,
    ) -> list[FusionCandidate]:
        """Gather and score all candidates for a (farm, bucket_date).

        Override in subclass for index-specific candidate gathering.

        Args:
            farm_id: Farm to gather candidates for.
            bucket_date: Date bucket to gather for.
            index_type: Spectral index type filter.

        Returns:
            List of FusionCandidate objects that passed screening.
        """
        raise NotImplementedError

    @abstractmethod
    def select_candidate(
        self,
        candidates: list[FusionCandidate],
    ) -> FusionResult:
        """Apply the decision tree to select the best candidate.

        Args:
            candidates: List of candidates that passed screening.

        Returns:
            FusionResult with the selected candidate or None.
        """
        ...

    def fuse(
        self,
        farm_id: int,
        bucket_date: date,
        index_type: str,
    ) -> FusionResult:
        """Run the full fusion pipeline: gather, score, select.

        Args:
            farm_id: Farm to fuse for.
            bucket_date: Date bucket to fuse for.
            index_type: Spectral index type.

        Returns:
            FusionResult with the selected candidate.
        """
        candidates = self.gather_candidates(farm_id, bucket_date, index_type)
        return self.select_candidate(candidates)


# ── Registry ────────────────────────────────────────────────────────────

FUSION_ENGINES: dict[str, type[FusionEngine]] = {}
"""Registry mapping index type (e.g. "NDMI") to FusionEngine subclass."""


def register_fusion_engine(index_type: str) -> Any:
    """Decorator to register a FusionEngine subclass in FUSION_ENGINES."""

    def decorator(cls: type[FusionEngine]) -> type[FusionEngine]:
        FUSION_ENGINES[index_type] = cls
        logger.debug(
            "Registered fusion engine %s for index %s",
            cls.__name__,
            index_type,
        )
        return cls

    return decorator


def get_fusion_engine(index_type: str) -> FusionEngine:
    """Get an instance of the registered FusionEngine for an index."""
    cls = FUSION_ENGINES.get(index_type)
    if cls is None:
        raise KeyError(
            f"No FusionEngine registered for index_type={index_type!r}"
        )
    return cls()
