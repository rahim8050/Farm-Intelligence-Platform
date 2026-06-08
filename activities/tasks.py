"""Activity scheduling tasks.

This module provides Celery tasks for the activity scheduler.

Scheduler: polls due activities and dispatches to queue
Worker: executes activity via handler

Auth: Uses Django auth, Celery task isolation.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from asgiref.sync import async_to_sync
from celery import shared_task
from django.conf import settings
from django.core.cache import caches
from django.utils import timezone

from activities.handlers import (  # noqa: F401
    fertilizer,
    irrigation,
    vaccination,
)
from activities.handlers.base import (
    HandlerResult,
    PermanentHandlerError,
    TemporaryHandlerError,
)

# Import metrics
from activities.metrics import (  # noqa: F401
    activities_active,
    activities_dispatched,
    activities_scheduler_dispatch_latency_seconds,
    activities_scheduler_runs,
    activity_duration_seconds,
)

logger = logging.getLogger("activities")
_SCHEDULER_LOCK_KEY = "activities:scheduler:poll:lock"


def _correlation_id(activity: Any) -> str:
    metadata = getattr(activity, "metadata", None)
    if isinstance(metadata, dict):
        correlation_id = metadata.get("correlation_id")
        if correlation_id:
            return str(correlation_id)
    return ""


# Import metrics


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
    from config.celery_metrics import record_scheduler_run

    lock_ttl = int(getattr(settings, "ACTIVITY_SCHEDULER_LOCK_SECONDS", 50))
    scheduler_lock = caches["default"].add(
        _SCHEDULER_LOCK_KEY,
        "1",
        timeout=max(1, lock_ttl),
    )
    if not scheduler_lock:
        record_scheduler_run("failure")
        logger.info("scheduler_poll_skipped reason=lock_contention")
        return {"dispatched": 0, "scanned": 0, "locked": True}

    batch_size = getattr(settings, "ACTIVITY_SCHEDULER_BATCH_SIZE", 100)

    scheduler_start = time.monotonic()
    try:
        due_activities = Activity.objects.filter(
            status=Activity.Status.PENDING, next_due_at__lte=timezone.now()
        ).order_by("next_due_at")[:batch_size]

        dispatched = 0
        for activity in due_activities:
            try:
                activity, execution_id = _claim_and_dispatch(activity.id)
                if activity:
                    dispatched += 1
                    activities_dispatched.labels(
                        type=activity.type, status="success"
                    ).inc()
                    logger.info(
                        "dispatched activity_id=%d execution_id=%s "
                        "correlation_id=%s",
                        activity.id,
                        execution_id,
                        _correlation_id(activity) or "none",
                    )
            except Exception as e:
                activities_dispatched.labels(
                    type=activity.type, status="failure"
                ).inc()
                logger.warning(
                    "Failed to dispatch activity %d: %s",
                    activity.id,
                    e,
                )

        activities_scheduler_runs.labels(status="success").inc()
        activities_scheduler_dispatch_latency_seconds.labels(
            status="success"
        ).observe(time.monotonic() - scheduler_start)

        return {
            "dispatched": dispatched,
            "scanned": len(due_activities),
        }
    except Exception:
        activities_scheduler_runs.labels(status="failure").inc()
        activities_scheduler_dispatch_latency_seconds.labels(
            status="failure"
        ).observe(time.monotonic() - scheduler_start)
        raise
    finally:
        caches["default"].delete(_SCHEDULER_LOCK_KEY)


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
    Emits WebSocket event on completion.

    Circuit breaker: if the handler type's breaker is open, dispatch is
    skipped and the activity is marked as failed with a clear message.

    Dead-letter: permanently failed activities are registered as dead
    letters for later replay.

    Handler exception hierarchy: ``PermanentHandlerError`` avoids
    unnecessary retries; ``TemporaryHandlerError`` allows retry.

    Args:
        activity_id: Activity ID to execute.
        execution_id: Execution ID for idempotency validation.

    Returns:
        Dict with status and result.
    """
    from activities.consumers import emit_activity_event
    from activities.services import (
        reschedule_recurring,
        transition_to_failed,
        transition_to_running,
        transition_to_success,
        validate_execution,
    )

    activity = validate_execution(activity_id, execution_id)
    correlation_id = _correlation_id(activity)
    handler_type = activity.type

    # Circuit breaker check
    from activities.circuit_breaker import (
        can_try_half_open,
        is_open,
        record_failure,
        record_success,
    )

    if is_open(handler_type):
        if not can_try_half_open(handler_type):
            err = f"Circuit breaker open for {handler_type} — dispatch blocked"
            transition_to_failed(activity, err)
            from activities.dead_letter import register_dead_letter

            register_dead_letter(
                activity.id,
                reason="circuit_breaker_open",
                activity_type=handler_type,
                error=err,
            )
            logger.warning(
                "activity_circuit_blocked activity_id=%d type=%s "
                "correlation_id=%s",
                activity.id,
                handler_type,
                correlation_id or "none",
            )
            return {
                "status": "circuit_blocked",
                "activity_id": activity_id,
                "result": err,
            }

    transition_to_running(activity)

    from django.db import transaction

    start_time = time.time()
    try:
        handler = _get_handler(activity.type)
        result = handler.execute(activity)

        record_success(handler_type)

        transition_to_success(activity)
        activity.refresh_from_db()

        # Handle HandlerResult or string return
        if isinstance(result, HandlerResult):
            message = result.message
            metadata = result.metadata or {}
        else:
            message = str(result)
            metadata = {}

        # Conditional recurrence: handler metadata can block reschedule
        reschedule_recurring(activity, handler_result_metadata=metadata)

        # Activity chaining from NDVI recommendations
        if handler_type == "ndvi_trigger" and metadata.get(
            "recommended_actions"
        ):
            _chain_ndvi_actions(activity, metadata)

        duration = time.time() - start_time
        activity_duration_seconds.labels(type=activity.type).observe(duration)

        logger.info(
            "activity_executed activity_id=%d type=%s result=%s "
            "correlation_id=%s",
            activity.id,
            activity.type,
            message,
            correlation_id or "none",
        )

        # Emit WebSocket event after DB commit
        def emit_event() -> None:
            async_to_sync(emit_activity_event)(
                activity.owner_id,
                {
                    "activity_id": activity.id,
                    "activity_type": activity.type,
                    "action": "completed",
                    "farm_id": activity.farm_id,
                    "message": message,
                    "metadata": metadata,
                    "correlation_id": correlation_id or None,
                    "timestamp": timezone.now().isoformat(),
                    "schema_version": "1.0",
                },
            )

        def emit_audio_alert() -> None:
            try:
                from alerts.triggers import on_activity_completed

                on_activity_completed(
                    user_id=activity.owner_id,
                    farm_id=activity.farm_id,
                    activity_id=activity.id,
                    activity_type=activity.type,
                    status="success",
                    message=message,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "audio alert hook failed for activity %d: %s",
                    activity.id,
                    exc.__class__.__name__,
                )

        transaction.on_commit(emit_event)
        transaction.on_commit(emit_audio_alert)

        return {
            "status": "success",
            "activity_id": activity_id,
            "result": message,
        }

    except PermanentHandlerError as e:
        err = str(e)
        transition_to_failed(activity, err)
        logger.error(
            "activity_permanent_failure activity_id=%d type=%s error=%s "
            "correlation_id=%s",
            activity.id,
            activity.type,
            err,
            correlation_id or "none",
        )

        from activities.dead_letter import register_dead_letter

        register_dead_letter(
            activity.id,
            reason="permanent_handler_failure",
            activity_type=handler_type,
            error=err,
            metadata=e.metadata,
        )
        return {
            "status": "permanent_failure",
            "activity_id": activity_id,
            "result": err,
        }

    except TemporaryHandlerError as e:
        err = str(e)
        record_failure(handler_type)
        transition_to_failed(activity, err)
        logger.warning(
            "activity_temporary_failure activity_id=%d type=%s error=%s "
            "correlation_id=%s",
            activity.id,
            activity.type,
            err,
            correlation_id or "none",
        )

        def emit_audio_alert_failed() -> None:
            try:
                from alerts.triggers import on_activity_completed

                on_activity_completed(
                    user_id=activity.owner_id,
                    farm_id=activity.farm_id,
                    activity_id=activity.id,
                    activity_type=activity.type,
                    status="failed",
                    message=err,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "audio alert hook failed for activity %d: %s",
                    activity.id,
                    exc.__class__.__name__,
                )

        transaction.on_commit(emit_audio_alert_failed)
        raise

    except ImportError:
        raise

    except Exception as e:
        err = str(e)
        record_failure(handler_type)
        transition_to_failed(activity, err)
        logger.error(
            "activity_failed activity_id=%d type=%s error=%s "
            "correlation_id=%s",
            activity.id,
            activity.type,
            e,
            correlation_id or "none",
        )

        def emit_audio_alert_failed() -> None:
            try:
                from alerts.triggers import on_activity_completed

                on_activity_completed(
                    user_id=activity.owner_id,
                    farm_id=activity.farm_id,
                    activity_id=activity.id,
                    activity_type=activity.type,
                    status="failed",
                    message=err,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "audio alert hook failed for activity %d: %s",
                    activity.id,
                    exc.__class__.__name__,
                )

        transaction.on_commit(emit_audio_alert_failed)
        raise


def _chain_ndvi_actions(activity: Any, metadata: dict[str, Any]) -> None:
    """Chain NDVI recommended actions as follow-up activities.

    Called after a successful ndvi_trigger execution. Creates one
    PENDING activity per recommended action linked to the same farm.
    """
    from activities.metrics import activities_chained_count
    from activities.services import chain_activity

    actions: list[str] = metadata.get("recommended_actions") or []
    for action_type in actions:
        chained = chain_activity(
            activity,
            action_type,
            metadata={
                "triggered_by_ndvi": True,
                "source_ndvi_state": metadata.get("state"),
                "source_farm_id": metadata.get("farm_id"),
            },
        )
        if chained:
            activities_chained_count.labels(
                source_type="ndvi_trigger",
                target_type=action_type,
            ).inc()
            logger.info(
                "ndvi_chained_action activity_id=%d target_type=%s new_id=%d",
                activity.id,
                action_type,
                chained.id,
            )


def _validate_and_execute(activity_id: int, execution_id: str) -> Any:
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

    stale_threshold = getattr(settings, "ACTIVITY_STALE_THRESHOLD_MINUTES", 30)

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


@shared_task(
    bind=True,
    name="activities.cleanup_completed",
)
def cleanup_completed_activities_task(self: Any) -> dict[str, Any]:
    """Remove old terminal activities."""
    from activities.services import cleanup_completed_activities

    retention_days = getattr(settings, "ACTIVITY_RETENTION_DAYS", 30)
    deleted = cleanup_completed_activities(older_than_days=retention_days)
    logger.info(
        "cleanup_completed_activities deleted=%d retention_days=%d",
        deleted,
        retention_days,
    )
    return {
        "deleted": deleted,
        "retention_days": retention_days,
    }
