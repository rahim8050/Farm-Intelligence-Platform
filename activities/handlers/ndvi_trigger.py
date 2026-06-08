"""NDVI trigger activity handler.

This handler responds to NDVI state changes by triggering follow-up farm
operations based on the current farm state classification.

Farm state integration:
- Reads farm state from ndvi.farm_state.build_farm_state()
- Triggers follow-up activities based on state classification
- Tracks state transitions to avoid duplicate actions

Activity types:
- ndvi_trigger: Event-driven activity based on NDVI state

Metadata expected:
- farm_id: int (required)
- engine: str (optional, defaults to sentinelhub)
- action_on_state: dict (optional, maps state -> activity_type)

Idempotency:
- Uses Redis lock to prevent duplicate execution within configured window
- Tracks previous state to avoid repeated actions on same state

Security:
- Validates metadata schema
- Action allowlist prevents arbitrary action injection
"""

from __future__ import annotations

import json
import logging
import time
from enum import StrEnum
from typing import TYPE_CHECKING, TypedDict

from django.core.cache import cache
from django.db import close_old_connections

from activities.handlers.base import HandlerResult
from activities.handlers.registry import register_handler
from activities.metrics import activities_ndvi_event_count

if TYPE_CHECKING:
    from activities.models import Activity

from farms.models import Farm

logger = logging.getLogger(__name__)


class FarmState(StrEnum):
    """Valid NDVI farm states."""

    ESTABLISHMENT = "establishment"
    FULL_CANOPY = "full_canopy"
    DECLINE = "decline"
    GROWTH = "growth"
    UNKNOWN = "unknown"


class RecommendedAction(StrEnum):
    """Valid recommended actions."""

    FERTILIZER = "fertilizer"
    IRRIGATION = "irrigation"
    VACCINATION = "vaccination"


ALLOWED_ACTIONS: frozenset[str] = frozenset(
    action.value for action in RecommendedAction
)

DEFAULT_STATE_ACTIONS: dict[str, list[str]] = {
    FarmState.ESTABLISHMENT.value: [
        RecommendedAction.FERTILIZER.value,
        RecommendedAction.IRRIGATION.value,
    ],
    FarmState.FULL_CANOPY.value: [RecommendedAction.FERTILIZER.value],
    FarmState.DECLINE.value: [
        RecommendedAction.IRRIGATION.value,
        RecommendedAction.VACCINATION.value,
    ],
    FarmState.GROWTH.value: [],
    FarmState.UNKNOWN.value: [],
}

IDEMPOTENCY_KEY_PREFIX = "ndvi_trigger:idempotency:"
IDEMPOTENCY_TTL_SECONDS = 300


class FarmStatePayload(TypedDict):
    """Farm state payload from build_farm_state."""

    farm_id: int
    state: str
    mean_ndvi: float | None
    max_ndvi: float | None
    coverage_pct: float | None
    trend: float | None
    interpretation: str
    action: str


class MetadataSchema(TypedDict, total=False):
    """Expected metadata schema for ndvi_trigger activity."""

    farm_id: int
    engine: str
    action_on_state: dict[str, list[str]]


class NdviTriggerHandler:
    """Handler for NDVI-triggered farm operations.

    This handler:
    1. Reads the current farm state from ndvi.farm_state
    2. Classifies the state and determines appropriate follow-up actions
    3. Logs the state and recommended actions
    4. Does NOT create new activities (dispatch is handled externally)

    Idempotency:
    - Uses Redis lock to prevent duplicate execution
    - Tracks state transitions to avoid repeated actions

    Security:
    - Validates metadata schema
    - Action allowlist prevents arbitrary injection
    """

    type = "ndvi_trigger"

    def __init__(self) -> None:
        self._activity_id: int | None = None

    def execute(self, activity: Activity) -> HandlerResult:
        """Execute NDVI trigger activity.

        Args:
            activity: Activity instance with metadata containing farm_id
                and optional engine/parameters.

        Returns:
            HandlerResult with success status, state classification,
            and recommended follow-up actions.
        """
        self._activity_id = getattr(activity, "id", None)

        try:
            return self._execute_impl(activity)
        finally:
            close_old_connections()

    def _execute_impl(self, activity: Activity) -> HandlerResult:
        """Internal implementation with explicit exception handling."""
        if not self._validate_metadata(activity):
            return HandlerResult(
                success=False,
                message="Invalid metadata schema",
                metadata={"error": "invalid_metadata"},
            )

        farm_id = self._get_farm_id(activity)
        if farm_id is None:
            return HandlerResult(
                success=False,
                message="No farm_id in activity metadata",
                metadata={"error": "missing_farm_id"},
            )

        if not self._check_idempotency(farm_id):
            logger.warning(
                "ndvi_trigger.duplicate_detected farm_id=%s activity_id=%s",
                farm_id,
                self._activity_id,
            )
            return HandlerResult(
                success=False,
                message="Duplicate execution detected",
                metadata={
                    "error": "duplicate_execution",
                    "farm_id": farm_id,
                },
            )

        engine = self._get_engine(activity.metadata)
        action_mapping = self._get_action_mapping(activity.metadata)

        try:
            farm_state = self._get_farm_state(farm_id, engine)
        except Farm.DoesNotExist:
            return HandlerResult(
                success=False,
                message="Farm not found",
                metadata={"error": "farm_not_found", "farm_id": farm_id},
            )
        except Exception as exc:
            logger.exception(
                "ndvi_trigger.farm_state_error farm_id=%s error=%s",
                farm_id,
                exc,
            )
            return HandlerResult(
                success=False,
                message=f"Failed to get farm state: {exc}",
                metadata={"error": "farm_state_error", "farm_id": farm_id},
            )

        if not self._check_state_transition(farm_id, farm_state["state"]):
            logger.info(
                "ndvi_trigger.no_transition farm_id=%s current_state=%s",
                farm_id,
                farm_state["state"],
            )
            return HandlerResult(
                success=True,
                message=f"No state transition: {farm_state['state']}",
                metadata={
                    "farm_id": farm_id,
                    "state": farm_state["state"],
                    "recommended_actions": [],
                    "no_transition": True,
                },
            )

        raw_actions = action_mapping.get(farm_state["state"], [])
        validated_actions = self._validate_actions(raw_actions)

        logger.info(
            "ndvi_state_evaluated",
            extra={
                "farm_id": farm_id,
                "activity_id": self._activity_id,
                "state": farm_state["state"],
                "recommended_actions": validated_actions,
                "mean_ndvi": farm_state.get("mean_ndvi"),
            },
        )

        return HandlerResult(
            success=True,
            message=f"NDVI state: {farm_state['state']}",
            metadata={
                "farm_id": farm_id,
                "state": farm_state["state"],
                "mean_ndvi": farm_state.get("mean_ndvi"),
                "max_ndvi": farm_state.get("max_ndvi"),
                "coverage_pct": farm_state.get("coverage_pct"),
                "trend": farm_state.get("trend"),
                "recommended_actions": validated_actions,
                "interpretation": farm_state.get("interpretation", ""),
                "action": farm_state.get("action", ""),
            },
        )

    def _validate_metadata(self, activity: Activity) -> bool:
        """Validate metadata schema."""
        metadata = getattr(activity, "metadata", None) or {}

        if not isinstance(metadata, dict):
            logger.warning(
                "ndvi_trigger.invalid_metadata_type metadata_type=%s",
                type(metadata).__name__,
            )
            return False

        action_on_state = metadata.get("action_on_state")
        if action_on_state is not None and not isinstance(
            action_on_state, dict
        ):
            logger.warning(
                "ndvi_trigger.invalid_action_on_state_type type=%s",
                type(action_on_state).__name__,
            )
            return False

        if action_on_state:
            for state, actions in action_on_state.items():
                if not isinstance(actions, list):
                    logger.warning(
                        "ndvi_trigger.invalid_actions_for_state state=%s",
                        state,
                    )
                    return False

        return True

    def _get_farm_id(self, activity: Activity) -> int | None:
        """Extract farm_id from activity metadata."""
        metadata = getattr(activity, "metadata", None) or {}
        farm_id = metadata.get("farm_id")

        if farm_id is not None:
            try:
                return int(farm_id)
            except (ValueError, TypeError) as exc:
                logger.warning(
                    "ndvi_trigger.invalid_farm_id farm_id=%s error=%s",
                    farm_id,
                    exc,
                )
                return None

        farm_id_attr = getattr(activity, "farm_id", None)
        if farm_id_attr is not None:
            return int(farm_id_attr)

        return None

    def _get_engine(self, metadata: dict) -> str:
        """Extract engine from metadata, default to sentinelhub."""
        return metadata.get("engine", "sentinelhub")

    def _get_action_mapping(self, metadata: dict) -> dict[str, list[str]]:
        """Get state-to-action mapping from metadata or use defaults."""
        custom_mapping = metadata.get("action_on_state")
        if custom_mapping and isinstance(custom_mapping, dict):
            return custom_mapping
        return DEFAULT_STATE_ACTIONS.copy()

    def _get_farm_state(self, farm_id: int, engine: str) -> FarmStatePayload:
        """Get farm state from ndvi.farm_state module."""
        farm = Farm.objects.get(id=farm_id)

        from ndvi.farm_state import build_farm_state

        result = build_farm_state(farm=farm, engine=engine)
        return result.as_payload()  # type: ignore[return-value]

    def _get_farm_state_legacy(
        self, farm_id: int, engine: str
    ) -> FarmStatePayload:
        """Get farm state from ndvi.farm_state module.

        Legacy version that raises exceptions for caller to handle.
        """
        from ndvi.farm_state import build_farm_state

        try:
            farm = Farm.objects.get(id=farm_id)
        except Farm.DoesNotExist:
            raise

        try:
            result = build_farm_state(farm=farm, engine=engine)
            return result.as_payload()  # type: ignore[return-value]
        except Exception as exc:
            logger.exception(
                "ndvi_trigger.farm_state_error farm_id=%s error=%s",
                farm_id,
                exc,
            )
            raise

    def _validate_actions(self, actions: list[str]) -> list[str]:
        """Validate actions against allowlist.

        Logs rejected actions but never propagates them.
        """
        validated = [action for action in actions if action in ALLOWED_ACTIONS]

        rejected = set(actions) - set(validated)
        if rejected:
            logger.warning(
                "ndvi_trigger.rejected_actions actions=%s allowed=%s",
                list(rejected),
                list(ALLOWED_ACTIONS),
            )

        return validated

    def _check_idempotency(self, farm_id: int) -> bool:
        """Check and set idempotency lock.

        Returns False if duplicate execution detected within TTL window.
        """
        key = f"{IDEMPOTENCY_KEY_PREFIX}{farm_id}"

        lock_key = f"{key}:lock"
        lock_acquired = cache.add(lock_key, "1", timeout=5)

        if not lock_acquired:
            cached_result = cache.get(key)
            if cached_result is not None:
                return False

        cache.set(
            key,
            json.dumps({"timestamp": time.time()}),
            IDEMPOTENCY_TTL_SECONDS,
        )

        if lock_acquired:
            cache.delete(lock_key)

        return True

    def _check_state_transition(
        self, farm_id: int, current_state: str
    ) -> bool:
        """Check if state changed from previous.

        Returns True if this is a new state or first evaluation.
        """
        key = f"ndvi_trigger:prev_state:{farm_id}"
        previous_state = cache.get(key)

        if previous_state is None:
            cache.set(key, current_state, timeout=86400)
            return True

        if previous_state != current_state:
            cache.set(key, current_state, timeout=86400)
            return True

        return False


# ---------------------------------------------------------------------------
# NDVI event listener — hook into NDVI job/completion events
# ---------------------------------------------------------------------------


def on_ndvi_job_completed(
    *,
    farm_id: int,
    engine: str = "sentinelhub",
    mean_ndvi: float | None = None,
    state: str | None = None,
    metadata: dict | None = None,
) -> dict[str, object]:
    """Event listener called when an NDVI job completes.

    This is the integration point between the NDVI processing pipeline
    and the activity scheduler. It creates or triggers ndvi_trigger
    activities for the farm whose NDVI state has been refreshed.

    This function is idempotent within a short window — consecutive
    calls for the same farm_id within the idempotency TTL are silently
    skipped.

    Args:
        farm_id: The farm whose NDVI state was refreshed.
        engine: The NDVI engine used.
        mean_ndvi: Optional mean NDVI value from the job.
        state: Optional farm state classification.
        metadata: Optional additional context.

    Returns:
        Dict with keys ``triggered`` (bool), ``activity_id`` (int|None),
        and ``message`` (str).
    """
    from activities.models import Activity
    from activities.services import chain_activity

    meta = dict(metadata or {})
    meta["farm_id"] = farm_id
    meta["engine"] = engine
    meta["source"] = "ndvi_job_completed"
    if mean_ndvi is not None:
        meta["mean_ndvi"] = mean_ndvi
    if state is not None:
        meta["state"] = state

    # Check idempotency: avoid creating duplicate triggers for same
    # farm within the TTL window
    idem_key = f"ndvi_event:listener:{farm_id}:{engine}"
    if cache.get(idem_key):
        activities_ndvi_event_count.labels(
            event_type="job_completed", status="duplicate"
        ).inc()
        return {
            "triggered": False,
            "activity_id": None,
            "message": "duplicate",
        }

    cache.set(idem_key, "1", timeout=IDEMPOTENCY_TTL_SECONDS)

    # Find owner from farm (first user who has an activity for this farm)
    farm_activities = (
        Activity.objects.filter(farm_id=farm_id, type="ndvi_trigger")
        .select_related("owner")
        .order_by("-created_at")[:1]
    )

    if not farm_activities:
        activities_ndvi_event_count.labels(
            event_type="job_completed", status="no_owner"
        ).inc()
        return {
            "triggered": False,
            "activity_id": None,
            "message": "no ndvi_trigger activity found for farm",
        }

    source = farm_activities[0]
    chained = chain_activity(
        source,
        "ndvi_trigger",
        metadata=meta,
    )
    if chained:
        activities_ndvi_event_count.labels(
            event_type="job_completed", status="triggered"
        ).inc()
        logger.info(
            "ndvi_event_triggered farm_id=%d activity_id=%d",
            farm_id,
            chained.id,
        )
        return {
            "triggered": True,
            "activity_id": chained.id,
            "message": "ndvi_trigger activity created",
        }

    return {
        "triggered": False,
        "activity_id": None,
        "message": "no owner found for farm",
    }


register_handler(NdviTriggerHandler)
