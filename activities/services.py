"""Activity service layer for state machine and execution.

Provides atomic claim, state transitions, and execution validation per
prompts/harden.md execution model freeze.

State transitions MUST only occur via this service layer.
Direct model mutation of status is forbidden.

Exports:
    claim_activity: Atomic activity claim for execution.
    validate_execution: Validate execution_id for idempotency.
    ActivityStateMachine: Enforces allowed state transitions.
    InvalidTransitionError: Raised on invalid transitions.
    StaleExecutionError: Raised on execution_id mismatch.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

if TYPE_CHECKING:
    from activities.models import Activity


class ActivityMachineError(Exception):
    """Base exception for activity state machine errors."""

    pass


class InvalidTransitionError(ActivityMachineError):
    """Raised when an invalid state transition is attempted."""

    pass


class StaleExecutionError(ActivityMachineError):
    """Raised when execution_id does not match."""

    pass


class ActivityStateMachine:
    """Enforces strict state transitions per prompts/harden.md.

    State transitions MUST ONLY occur via this service layer.
    Direct model mutation of status is forbidden.
    """

    _ALLOWED_TRANSITIONS: dict = None

    @classmethod
    def _ensure_transitions(cls) -> None:
        """Lazy initialization of transitions after Activity is loaded."""
        if cls._ALLOWED_TRANSITIONS is None:
            from activities.models import Activity

            cls._ALLOWED_TRANSITIONS = {
                Activity.Status.PENDING: [Activity.Status.DISPATCHED],
                Activity.Status.DISPATCHED: [Activity.Status.RUNNING],
                Activity.Status.RUNNING: [
                    Activity.Status.SUCCESS,
                    Activity.Status.FAILED,
                    Activity.Status.RETRY,
                ],
                Activity.Status.RETRY: [Activity.Status.PENDING],
                Activity.Status.SUCCESS: [],
                Activity.Status.FAILED: [Activity.Status.PENDING],
            }

    @classmethod
    def can_transition(cls, current: str, new: str) -> bool:
        """Check if transition is allowed."""
        from activities.models import Activity

        cls._ensure_transitions()

        # Handle both strings and enum values
        if isinstance(current, str):
            current_status = Activity.Status(current)
        else:
            current_status = current

        if isinstance(new, str):
            new_status = Activity.Status(new)
        else:
            new_status = new

        return new_status in cls._ALLOWED_TRANSITIONS.get(current_status, [])

    @classmethod
    @transaction.atomic
    def transition(cls, activity: Activity, new_status: str) -> Activity:
        """Transition activity to new status.

        Raises:
            InvalidTransitionError: If transition is not allowed.
        """
        cls._ensure_transitions()
        if not cls.can_transition(activity.status, new_status):
            raise InvalidTransitionError(
                f"Invalid transition {activity.status} -> {new_status}"
            )

        activity.status = new_status
        activity.save(update_fields=["status", "updated_at"])
        return activity


@transaction.atomic
def claim_activity(
    activity_id: int,
) -> tuple[Activity | None, str | None]:
    """Atomically claim an activity for execution.

    Uses atomic UPDATE with status=PENDING condition.
    No SELECT-then-UPDATE pattern.

    Returns:
        (Activity, execution_id) if claimed, (None, None) if contention.
    """
    from activities.models import Activity

    execution_id = uuid.uuid4()

    updated = Activity.objects.filter(
        id=activity_id,
        status=Activity.Status.PENDING,
    ).update(
        status=Activity.Status.DISPATCHED,
        execution_id=execution_id,
        execution_started_at=timezone.now(),
    )

    if not updated:
        return None, None

    activity = Activity.objects.get(id=activity_id)
    return activity, str(execution_id)


def validate_execution(activity_id: int, execution_id: str) -> Activity:
    """Validate execution ownership via execution_id.

    Worker MUST call this before processing.
    Aborts if execution_id does not match (stale execution).
    Rejects if status is not DISPATCHED/RUNNING.

    Raises:
        StaleExecutionError: If execution_id mismatch or None.
        InvalidTransitionError: If status is terminal.
    """
    from activities.models import Activity

    try:
        activity = Activity.objects.get(id=activity_id)
    except Activity.DoesNotExist as e:
        raise StaleExecutionError("Activity not found") from e

    if activity.status not in [
        Activity.Status.DISPATCHED,
        Activity.Status.RUNNING,
    ]:
        raise InvalidTransitionError(
            f"Cannot execute: status is {activity.status}"
        )

    if activity.execution_id is None:
        raise StaleExecutionError("execution_id is None - not claimed")

    if str(activity.execution_id) != execution_id:
        raise StaleExecutionError(
            f"Stale execution - expected {activity.execution_id}, "
            f"got {execution_id}"
        )

    return activity


def transition_to_running(activity: Activity) -> Activity:
    """Transition DISPATCHED -> RUNNING."""
    from activities.models import Activity

    return ActivityStateMachine.transition(activity, Activity.Status.RUNNING)


def transition_to_success(activity: Activity) -> Activity:
    """Transition RUNNING -> SUCCESS."""
    from activities.models import Activity

    activity.status = Activity.Status.SUCCESS
    activity.execution_completed_at = timezone.now()
    activity.save(
        update_fields=["status", "execution_completed_at", "updated_at"]
    )
    return activity


def transition_to_failed(activity: Activity, error: str) -> Activity:
    """Transition RUNNING -> FAILED."""
    from activities.models import Activity

    activity.status = Activity.Status.FAILED
    activity.last_error = error
    activity.execution_completed_at = timezone.now()
    activity.save(
        update_fields=[
            "status",
            "last_error",
            "execution_completed_at",
            "updated_at",
        ],
    )
    return activity


def transition_to_retry(activity: Activity, next_due_at: datetime) -> Activity:
    """Transition RUNNING -> RETRY with backoff."""
    from activities.models import Activity

    if activity.retry_count >= activity.max_retries:
        activity.status = Activity.Status.FAILED
        activity.last_error = "Max retries exceeded"
    else:
        activity.status = Activity.Status.RETRY
        activity.retry_count += 1
        activity.next_due_at = next_due_at

    activity.save(
        update_fields=[
            "status",
            "retry_count",
            "next_due_at",
            "last_error",
            "updated_at",
        ],
    )
    return activity


@transaction.atomic
def recover_stale_activity(activity: Activity) -> Activity:
    """Recover stale DISPATCHED or RUNNING activities.

    Called by recovery task to reset stuck activities.
    Uses select_for_update to prevent worker collisions.
    Clears execution_id to prevent stale execution reuse.
    """
    from activities.models import Activity

    activity = (
        Activity.objects.filter(id=activity.id).select_for_update().first()
    )

    if not activity:
        return activity

    if activity.status not in [
        Activity.Status.DISPATCHED,
        Activity.Status.RUNNING,
    ]:
        return activity  # Worker finished normally

    activity.execution_id = None
    activity.execution_started_at = None

    if activity.retry_count < activity.max_retries:
        activity.status = Activity.Status.RETRY
        activity.retry_count += 1
    else:
        activity.status = Activity.Status.FAILED
        activity.last_error = "Max retries exceeded in recovery"

    activity.save(
        update_fields=[
            "status",
            "execution_id",
            "execution_started_at",
            "retry_count",
            "last_error",
            "updated_at",
        ],
    )
    return activity
