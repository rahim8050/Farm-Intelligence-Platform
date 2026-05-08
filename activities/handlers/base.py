"""Base classes and types for activity handlers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from activities.tasks import ActivityHandler


@dataclass
class HandlerResult:
    """Result of activity handler execution."""

    success: bool
    message: str
    metadata: dict | None = None


class ActivityHandler:
    """Base handler for activity types.

    Subclasses must implement execute().
    """

    type: str = "base"

    def execute(self, activity: Any) -> str:
        """Execute the activity.

        Args:
            activity: Activity instance.

        Returns:
            Result message.
        """
        return f"Executed {self.type}"
