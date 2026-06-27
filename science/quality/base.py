"""Abstract quality scorer base class.

This module defines the base QualityScorer interface that all index-specific
quality scorers must implement. Each scorer converts raw V1 observations
into scored V2 observations with confidence metrics, null detection, and
temporal smoothing.

Auth: No authentication — called from Celery tasks.
Response: QualityResult dataclass (not wrapped in API envelope).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class QualityResult:
    """Output of a quality scoring operation.

    Attributes:
        selected_value: The accepted index value (None if nulled).
        smoothed_value: Temporally smoothed value (None if nulled or
            insufficient context).
        confidence: Composite confidence score in [0.0, 1.0].
        confidence_components: Breakdown of confidence sub-scores.
        quality_flags: Dict of boolean quality indicators.
        is_null: Whether the observation was rejected (nulled).
        null_reason: Reason for nulling, if is_null is True.
    """

    selected_value: float | None
    smoothed_value: float | None
    confidence: float
    confidence_components: dict[str, float] = field(default_factory=dict)
    quality_flags: dict[str, bool] = field(default_factory=dict)
    is_null: bool = False
    null_reason: str | None = None


class QualityScorer(ABC):
    """Abstract base class for index-specific quality scorers.

    Subclasses define per-index thresholds, source weights, confidence
    formulas, and null-return conditions.

    The lifecycle is:
        1. score() — convert a V1 observation to a V2 QualityResult.
        2. persist() — write the result to NdviDerivedObservation.
    """

    # Override in subclasses
    SOURCE_WEIGHTS: dict[str, float] = {}
    CONFIDENCE_WEIGHTS: dict[str, float] = {}

    @abstractmethod
    def score(
        self,
        observation: Any,
        *,
        prior_values: list[float] | None = None,
    ) -> QualityResult:
        """Score a V1 observation and produce a V2 quality result.

        Args:
            observation: The V1 observation instance (has .mean, .engine,
                .cloud_fraction, .valid_pixel_fraction, .acquired_at,
                .bucket_date, .quality_flags, .farm_id).
            prior_values: Optional pre-fetched prior V2 values for rolling
                median computation. If None, implementors should fetch from
                the database.

        Returns:
            QualityResult with scored value, confidence, and flags.
        """
        ...

    @abstractmethod
    def persist(
        self,
        observation: Any,
        result: QualityResult,
        index_type: str,
    ) -> Any:
        """Persist a scored quality result to NdviDerivedObservation.

        Args:
            observation: The source V1 observation.
            result: The computed QualityResult.
            index_type: The spectral index type (e.g. "NDMI").

        Returns:
            The persisted NdviDerivedObservation instance.
        """
        ...

    @classmethod
    def process_v1_to_v2(
        cls,
        observation: Any,
        *,
        persist: bool = True,
        index_type: str | None = None,
    ) -> tuple[QualityResult, Any | None]:
        """Full pipeline: score + optionally persist.

        Convenience classmethod that calls score() then optionally
        persist(). Subclasses can override if they need custom flow.

        Args:
            observation: The V1 observation to process.
            persist: Whether to persist the result.
            index_type: The index type label (defaults to subclass name).

        Returns:
            Tuple of (QualityResult, persisted object or None).
        """
        scorer = cls()
        result = scorer.score(observation)
        persisted = None
        if persist:
            idx_type = (
                index_type or cls.__name__.replace("QualityScorer", "").upper()
            )  # noqa: E501
            persisted = scorer.persist(observation, result, idx_type)
        return result, persisted


# ── Registry ────────────────────────────────────────────────────────────

QUALITY_SCORERS: dict[str, type[QualityScorer]] = {}
"""Registry mapping index type (e.g. "NDMI") to QualityScorer subclass."""


def register_quality_scorer(index_type: str) -> Any:
    """Decorator to register a QualityScorer subclass in QUALITY_SCORERS."""

    def decorator(cls: type[QualityScorer]) -> type[QualityScorer]:
        QUALITY_SCORERS[index_type] = cls
        logger.debug(
            "Registered quality scorer %s for index %s",
            cls.__name__,
            index_type,
        )
        return cls

    return decorator


def get_quality_scorer(index_type: str) -> QualityScorer:
    """Get an instance of the registered QualityScorer for an index."""
    cls = QUALITY_SCORERS.get(index_type)
    if cls is None:
        raise KeyError(
            f"No QualityScorer registered for index_type={index_type!r}"
        )
    return cls()
