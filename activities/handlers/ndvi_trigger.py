"""NDVI trigger activity handler.

This handler responds to NDVI state changes by triggering follow-up farm
operations based on the current farm state classification.

Farm state integration:
- Reads farm state from ndvi.farm_state.build_farm_state()
- Triggers follow-up activities based on state classification
- Dispatches farm state coverage computation if needed

Activity types:
- ndvi_trigger: Event-driven activity based on NDVI state

Metadata expected:
- farm_id: int (required)
- engine: str (optional, defaults to sentinelhub)
- action_on_state: dict (optional, maps state -> activity_type)
"""

from __future__ import annotations

import logging
from typing import Any

from django.db import close_old_connections

from activities.handlers.base import HandlerResult
from activities.handlers.registry import register_handler
from activities.models import Activity

logger = logging.getLogger(__name__)

STATE_ACTION_MAPPING: dict[str, list[str]] = {
    "establishment": ["fertilizer", "irrigation"],
    "full_canopy": ["fertilizer"],
    "decline": ["irrigation", "vaccination"],
    "growth": [],
    "unknown": [],
}

DEFAULT_THRESHOLD = 0.3


class NdviTriggerHandler:
    """Handler for NDVI-triggered farm operations.

    This handler:
    1. Reads the current farm state from ndvi.farm_state
    2. Classifies the state and determines appropriate follow-up actions
    3. Logs the state and recommended actions
    4. Does NOT create new activities (dispatch is handled externally)

    The handler is idempotent - running it multiple times with the same
    farm state produces the same result.
    """

    type = "ndvi_trigger"

    def execute(self, activity: Activity) -> HandlerResult:
        """Execute NDVI trigger activity.

        Args:
            activity: Activity instance with metadata containing farm_id
                and optional engine/parameters.

        Returns:
            HandlerResult with success status, state classification,
            and recommended follow-up actions.
        """
        close_old_connections()

        farm_id = self._get_farm_id(activity)
        if farm_id is None:
            return HandlerResult(
                success=False,
                message="No farm_id in activity metadata",
                metadata={"error": "missing_farm_id"},
            )

        engine = self._get_engine(activity.metadata)
        action_mapping = self._get_action_mapping(activity.metadata)

        try:
            farm_state = self._get_farm_state(farm_id, engine)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ndvi_trigger.farm_state_error farm_id=%s error=%s",
                farm_id,
                exc,
            )
            return HandlerResult(
                success=False,
                message=f"Failed to get farm state: {exc}",
                metadata={"error": "farm_state_error", "farm_id": farm_id},
            )

        state = farm_state.get("state", "unknown")
        mean_ndvi = farm_state.get("mean_ndvi")
        max_ndvi = farm_state.get("max_ndvi")
        coverage_pct = farm_state.get("coverage_pct")
        trend = farm_state.get("trend")

        recommended_actions = action_mapping.get(state, [])

        logger.info(
            "ndvi_trigger.executed farm_id=%s state=%s mean_ndvi=%s "
            "recommended_actions=%s",
            farm_id,
            state,
            mean_ndvi,
            recommended_actions,
        )

        return HandlerResult(
            success=True,
            message=f"NDVI state: {state}",
            metadata={
                "farm_id": farm_id,
                "state": state,
                "mean_ndvi": mean_ndvi,
                "max_ndvi": max_ndvi,
                "coverage_pct": coverage_pct,
                "trend": trend,
                "recommended_actions": recommended_actions,
                "interpretation": farm_state.get("interpretation", ""),
                "action": farm_state.get("action", ""),
            },
        )

    def _get_farm_id(self, activity: Activity) -> int | None:
        """Extract farm_id from activity metadata."""
        farm_id = activity.metadata.get("farm_id")
        if farm_id is not None:
            return int(farm_id)

        if activity.farm_id is not None:
            return activity.farm_id

        return None

    def _get_engine(self, metadata: dict) -> str:
        """Extract engine from metadata, default to sentinelhub."""
        return metadata.get("engine", "sentinelhub")

    def _get_action_mapping(self, metadata: dict) -> dict[str, list[str]]:
        """Get state-to-action mapping from metadata or use defaults."""
        custom_mapping = metadata.get("action_on_state")
        if custom_mapping and isinstance(custom_mapping, dict):
            return custom_mapping
        return STATE_ACTION_MAPPING.copy()

    def _get_farm_state(self, farm_id: int, engine: str) -> dict[str, Any]:
        """Get farm state from ndvi.farm_state module."""
        from farms.models import Farm

        farm = Farm.objects.get(id=farm_id)

        from ndvi.farm_state import build_farm_state

        result = build_farm_state(farm=farm, engine=engine)
        return result.as_payload()


register_handler(NdviTriggerHandler)
