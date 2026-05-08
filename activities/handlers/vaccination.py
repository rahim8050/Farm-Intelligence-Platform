"""Vaccination activity handler."""

from typing import Any

from activities.handlers.base import HandlerResult
from activities.handlers.registry import register_handler


class VaccinationHandler:
    """Handler for vaccination activities."""

    type = "vaccination"

    def execute(self, activity: Any) -> HandlerResult:
        """Execute vaccination activity.

        Args:
            activity: Activity instance.

        Returns:
            HandlerResult with success status and metadata.
        """
        cattle_id = activity.metadata.get("cattle_id", "unknown")
        return HandlerResult(
            success=True,
            message="Vaccination completed",
            metadata={"cattle_id": cattle_id},
        )


register_handler(VaccinationHandler)
