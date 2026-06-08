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

import logging
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from config.celery_metrics import record_lock_contention

logger = logging.getLogger("activities")

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


def claim_activity(
    activity_id: int,
) -> tuple[Activity | None, str | None]:
    """Atomically claim an activity for execution.

    Uses atomic UPDATE with status=PENDING condition.
    No SELECT-then-UPDATE pattern.
    No SELECT FOR UPDATE used.

    Correctness depends on:
    - status=PENDING condition in WHERE clause
    - Single UPDATE statement is atomic at DB level

    Multiple schedulers may attempt to claim same activity;
    the status=PENDING condition ensures only one succeeds.

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
        record_lock_contention("claim")
        logger.info(
            "activity_claim_skipped activity_id=%d reason=contention",
            activity_id,
        )
        return None, None

    activity = Activity.objects.get(id=activity_id)

    logger.info(
        "activity_claimed activity_id=%d execution_id=%s",
        activity_id,
        execution_id,
    )

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
        record_lock_contention("execute")
        raise InvalidTransitionError(
            f"Cannot execute: status is {activity.status}"
        )

    if activity.execution_id is None:
        record_lock_contention("execute")
        raise StaleExecutionError("execution_id is None - not claimed")

    if str(activity.execution_id) != execution_id:
        record_lock_contention("execute")
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


def reschedule_recurring(
    activity: Activity,
    *,
    handler_result_metadata: dict | None = None,
) -> Activity:
    """Reschedule a recurring activity after successful execution.

    For cron-recurring activities, computes the next due time and
    transitions the activity back to PENDING so the scheduler picks it up.
    For interval-recurring activities, does the same using interval_days.

    When ``handler_result_metadata`` contains a ``conditional_skip`` key
    with value ``True``, the reschedule is skipped (conditional recurrence
    gates on handler output).

    Returns:
        The updated activity, or the original if not recurring.
    """
    from activities.models import Activity as ActivityModel

    if activity.recurrence_type == ActivityModel.RecurrenceType.NONE:
        return activity

    result = handler_result_metadata or {}

    # Conditional recurrence: handler result says "don't reschedule"
    if result.get("conditional_skip"):
        logger.info(
            "reschedule_conditional_skip activity_id=%d type=%s",
            activity.id,
            activity.type,
        )
        return activity

    if activity.recurrence_type == ActivityModel.RecurrenceType.CRON:
        if not activity.cron_expression:
            return activity
        next_due = activity.__class__._compute_cron_next(
            activity.cron_expression, timezone.now()
        )
        activity.next_due_at = next_due
        activity.status = ActivityModel.Status.PENDING
        activity.save(update_fields=["status", "next_due_at", "updated_at"])
        logger.info(
            "rescheduled_cron activity_id=%d next_due_at=%s",
            activity.id,
            next_due,
        )
        return activity

    if activity.recurrence_type == ActivityModel.RecurrenceType.INTERVAL:
        if not activity.interval_days:
            return activity
        next_due = timezone.now() + timezone.timedelta(
            days=activity.interval_days
        )
        activity.next_due_at = next_due
        activity.status = ActivityModel.Status.PENDING
        activity.save(update_fields=["status", "next_due_at", "updated_at"])
        logger.info(
            "rescheduled_interval activity_id=%d next_due_at=%s",
            activity.id,
            next_due,
        )
        return activity

    return activity


def chain_activity(
    source: Activity,
    target_type: str,
    *,
    scheduled_at: datetime | None = None,
    metadata: dict | None = None,
) -> Activity | None:
    """Create a follow-up activity chained from a completed source activity.

    The chained activity is owned by the same user and linked to the same
    farm (if any). It starts in ``PENDING`` status so the scheduler picks
    it up on the next poll cycle.

    Returns:
        The newly created Activity, or ``None`` if the source has no owner.
    """
    from activities.models import Activity as ActivityModel

    owner = source.owner
    if owner is None:
        return None

    chain_meta = dict(metadata or {})
    chain_meta["chained_from"] = source.id
    chain_meta["chained_from_type"] = source.type

    activity = ActivityModel.objects.create(
        owner=owner,
        farm_id=source.farm_id,
        type=target_type,
        status=ActivityModel.Status.PENDING,
        scheduled_at=scheduled_at or timezone.now(),
        next_due_at=scheduled_at or timezone.now(),
        metadata=chain_meta,
    )
    logger.info(
        "chained_activity source_id=%d target_type=%s new_id=%d",
        source.id,
        target_type,
        activity.id,
    )
    return activity


def cleanup_completed_activities(
    *, older_than_days: int = 30, batch_size: int = 500
) -> int:
    """Delete old terminal activities.

    Removes completed SUCCESS or FAILED activities older than the
    retention window. This is a storage-management task only; it does not
    affect active scheduler or worker state.
    """
    from activities.models import Activity

    cutoff = timezone.now() - timezone.timedelta(days=older_than_days)
    eligible_ids = list(
        Activity.objects.filter(
            status__in=[Activity.Status.SUCCESS, Activity.Status.FAILED],
            updated_at__lt=cutoff,
        )
        .order_by("updated_at")
        .values_list("id", flat=True)[:batch_size]
    )
    if not eligible_ids:
        return 0
    deleted, _ = Activity.objects.filter(
        status__in=[Activity.Status.SUCCESS, Activity.Status.FAILED],
        id__in=eligible_ids,
    ).delete()
    return int(deleted)
