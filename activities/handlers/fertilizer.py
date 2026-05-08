"""Fertilizer activity handler."""

from typing import Any

from activities.handlers.base import HandlerResult
from activities.handlers.registry import register_handler


class FertilizerHandler:
    """Handler for fertilizer activities."""

    type = "fertilizer"

    def execute(self, activity: Any) -> HandlerResult:
        """Execute fertilizer activity.

        Args:
            activity: Activity instance.

        Returns:
            HandlerResult with success status and metadata.
        """
        amount_kg = activity.metadata.get("amount_kg", 0)
        fertilizer_type = activity.metadata.get("fertilizer_type", "unknown")
        return HandlerResult(
            success=True,
            message="Fertilizer applied",
            metadata={
                "amount_kg": amount_kg,
                "fertilizer_type": fertilizer_type,
            },
        )


register_handler(FertilizerHandler)
