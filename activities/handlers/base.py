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


class HandlerError(Exception):
    """Base exception for activity handler errors."""

    def __init__(
        self,
        message: str,
        *,
        temporary: bool = True,
        metadata: dict | None = None,
    ) -> None:
        self.temporary = temporary
        self.metadata = metadata or {}
        super().__init__(message)


class TemporaryHandlerError(HandlerError):
    """Transient handler failure — safe to retry.

    Examples: network timeouts, upstream API rate limits,
    database connection errors.
    """

    def __init__(self, message: str, *, metadata: dict | None = None) -> None:
        super().__init__(message, temporary=True, metadata=metadata)


class PermanentHandlerError(HandlerError):
    """Non-recoverable handler failure — must not retry.

    Examples: invalid metadata, missing farm, permission denied.
    """

    def __init__(self, message: str, *, metadata: dict | None = None) -> None:
        super().__init__(message, temporary=False, metadata=metadata)


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
