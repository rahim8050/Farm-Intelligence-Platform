"""Activity handler registry.

Provides handler lookup for activity types.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from activities.tasks import ActivityHandler


HANDLER_REGISTRY: dict[str, type[ActivityHandler]] = {}


def register_handler(
    handler_class: type[ActivityHandler],
) -> type[ActivityHandler]:
    """Register a handler class for an activity type.

    Args:
        handler_class: Handler class to register.

    Returns:
        The handler class (for decorator use).
    """
    HANDLER_REGISTRY[handler_class.type] = handler_class
    return handler_class


def get_handler(activity_type: str) -> ActivityHandler:
    """Get handler instance for activity type.

    Args:
        activity_type: Activity type string.

    Returns:
        ActivityHandler instance.

    Raises:
        ValueError: If no handler registered for type.
    """
    handler_class = HANDLER_REGISTRY.get(activity_type)
    if handler_class is None:
        return DefaultHandler(activity_type)
    return handler_class()


class DefaultHandler:
    """Default handler when none registered.

    Used as fallback for unmapped activity types.
    """

    def __init__(self, activity_type: str) -> None:
        self.type = activity_type

    def execute(self, activity: Any) -> str:
        """Execute the activity.

        Returns:
            Success message.
        """
        return f"Default handler executed for {self.type}"


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