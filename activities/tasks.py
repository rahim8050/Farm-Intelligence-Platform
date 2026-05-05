"""Activity scheduling tasks.

This module provides Celery tasks for the activity scheduler.

Scheduler: polls due activities and dispatches to queue
Worker: executes activity via handler

Auth: Uses Django auth, Celery task isolation.
"""

from __future__ import annotations

import logging
from typing import Any

from celery import shared_task
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger("activities")


@shared_task(
    bind=True,
    name="activities.scheduler.poll",
    max_retries=3,
    default_retry_delay=60,
)
def poll_activities(self: Any) -> dict[str, Any]:
    """Poll for due activities and dispatch to queue.

    Scheduled via Celery Beat every 60 seconds.
    Finds activities with status=PENDING and next_due_at <= now.
    Atomically claims each activity and dispatches to worker.

    Returns:
        Dict with dispatched count and scanned count.
    """
    from activities.models import Activity

    batch_size = getattr(
        settings, "ACTIVITY_SCHEDULER_BATCH_SIZE", 100
    )

    due_activities = Activity.objects.filter(
        status=Activity.Status.PENDING,
        next_due_at__lte=timezone.now()
    ).order_by("next_due_at")[:batch_size]

    dispatched = 0
    for activity in due_activities:
        try:
            activity, execution_id = _claim_and_dispatch(activity.id)
            if activity:
                dispatched += 1
                logger.info(
                    "dispatched activity_id=%d execution_id=%s",
                    activity.id,
                    execution_id,
                )
        except Exception as e:
            logger.warning(
                "Failed to dispatch activity %d: %s",
                activity.id,
                e,
            )

    return {
        "dispatched": dispatched,
        "scanned": len(due_activities),
    }


def _claim_and_dispatch(
    activity_id: int,
) -> tuple[Any, str | None]:
    """Atomically claim activity and dispatch to worker.

    Uses services.claim_activity for atomic claim.
    Then dispatches the execute_activity task.

    Returns:
        (Activity, execution_id) if claimed, (None, None) if contention.
    """
    from activities.services import claim_activity

    activity, execution_id = claim_activity(activity_id)

    if activity:
        execute_activity.delay(activity_id, execution_id)

    return activity, execution_id


@shared_task(
    bind=True,
    name="activities.execute",
    max_retries=3,
    default_retry_delay=60,
    time_limit=300,
    soft_time_limit=270,
)
def execute_activity(
    self: Any, activity_id: int, execution_id: str
) -> dict[str, Any]:
    """Execute activity via handler.

    Validates execution_id for idempotency.
    Transitions to RUNNING.
    Executes handler.
    Transitions to SUCCESS/FAILED.

    Args:
        activity_id: Activity ID to execute.
        execution_id: Execution ID for idempotency validation.

    Returns:
        Dict with status and result.
    """
    _validate_and_execute(activity_id, execution_id)

    return {
        "status": "success",
        "activity_id": activity_id,
    }


def _validate_and_execute(
    activity_id: int, execution_id: str
) -> Any:
    """Validate execution and run handler.

    Uses services.validate_execution for idempotency.
    Uses services.transition_to_* for state transitions.

    Returns:
        Activity after execution.
    """
    from activities.services import (
        transition_to_failed,
        transition_to_running,
        transition_to_success,
        validate_execution,
    )

    activity = validate_execution(activity_id, execution_id)

    transition_to_running(activity)

    try:
        handler = _get_handler(activity.type)
        result = handler.execute(activity)

        transition_to_success(activity)

        logger.info(
            "activity_executed activity_id=%d type=%s result=%s",
            activity.id,
            activity.type,
            result,
        )

        return activity

    except Exception as e:
        transition_to_failed(activity, str(e))
        logger.error(
            "activity_failed activity_id=%d type=%s error=%s",
            activity.id,
            activity.type,
            e,
        )
        raise


def _get_handler(activity_type: str) -> Any:
    """Get handler for activity type.

    Returns:
        ActivityHandler instance for the given type.
    """
    from activities.handlers import get_handler

    return get_handler(activity_type)


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


@shared_task(
    bind=True,
    name="activities.recover_stale",
)
def recover_stale_activities(self: Any) -> dict[str, Any]:
    """Recover stale DISPATCHED or RUNNING activities.

    Scheduled every 5 minutes.
    Finds activities stuck in DISPATCHED or RUNNING for too long.
    Transitions to RETRY or FAILED based on retry count.

    Returns:
        Dict with recovered count.
    """
    from activities.models import Activity
    from activities.services import recover_stale_activity

    stale_threshold = getattr(
        settings, "ACTIVITY_STALE_THRESHOLD_MINUTES", 30
    )

    stale_activities = Activity.objects.filter(
        status__in=[
            Activity.Status.DISPATCHED,
            Activity.Status.RUNNING,
        ],
        execution_started_at__lt=timezone.now()
        - timezone.timedelta(minutes=stale_threshold),
    )

    recovered = 0
    for activity in stale_activities:
        recover_stale_activity(activity)
        recovered += 1
        logger.info(
            "recovered_stale activity_id=%d status=%s",
            activity.id,
            activity.status,
        )

    return {"recovered": recovered}