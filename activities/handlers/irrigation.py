"""Irrigation activity handler."""

from typing import Any

from activities.handlers.base import HandlerResult
from activities.handlers.registry import register_handler


class IrrigationHandler:
    """Handler for irrigation activities."""

    type = "irrigation"

    def execute(self, activity: Any) -> HandlerResult:
        """Execute irrigation activity.

        Args:
            activity: Activity instance.

        Returns:
            HandlerResult with success status and metadata.
        """
        duration_min = activity.metadata.get("duration_min", 15)
        return HandlerResult(
            success=True,
            message="Irrigation completed",
            metadata={"duration_min": duration_min},
        )


register_handler(IrrigationHandler)
