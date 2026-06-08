"""Dead letter handling for permanently failed activities.

Activities that exhaust all retries or trigger permanent-failure
conditions are moved into a dead-letter queue. This module provides:

- Registration and storage of dead-letter entries (cache-backed)
- A replay mechanism to re-queue dead letters
- Diagnostics for monitoring

The dead-letter store uses the Django cache with a configurable TTL.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from django.core.cache import cache

from activities.metrics import activities_dead_letter_count

logger = logging.getLogger("activities")

DEAD_LETTER_PREFIX = "activities:dead_letter:"
DEAD_LETTER_INDEX_KEY = "activities:dead_letter:index"
DEAD_LETTER_TTL = 86400 * 7  # 7 days


def _dl_key(activity_id: int) -> str:
    return f"{DEAD_LETTER_PREFIX}{activity_id}"


def register_dead_letter(
    activity_id: int,
    reason: str,
    *,
    activity_type: str = "",
    error: str = "",
    metadata: dict[str, Any] | None = None,
) -> None:
    """Register a permanently failed activity as a dead letter entry.

    Idempotent — calling again with the same activity_id updates
    the existing entry.
    """
    entry = {
        "activity_id": activity_id,
        "activity_type": activity_type,
        "reason": reason,
        "error": error,
        "metadata": metadata or {},
        "registered_at": __import__("time").time(),
    }
    key = _dl_key(activity_id)
    cache.set(key, json.dumps(entry), timeout=DEAD_LETTER_TTL)

    _add_to_index(activity_id)
    activities_dead_letter_count.labels(type=activity_type or "unknown").inc()

    logger.warning(
        "dead_letter_registered activity_id=%d type=%s reason=%s",
        activity_id,
        activity_type,
        reason,
    )


def _add_to_index(activity_id: int) -> None:
    """Track activity_id in the dead-letter index set."""
    index: list[int] = cache.get(DEAD_LETTER_INDEX_KEY, [])
    if activity_id not in index:
        index.append(activity_id)
        cache.set(DEAD_LETTER_INDEX_KEY, index, timeout=DEAD_LETTER_TTL)


def list_dead_letters() -> list[dict[str, Any]]:
    """Return all registered dead-letter entries."""
    index: list[int] = cache.get(DEAD_LETTER_INDEX_KEY, [])
    entries: list[dict[str, Any]] = []
    for aid in index:
        raw = cache.get(_dl_key(aid))
        if raw:
            try:
                entries.append(json.loads(raw))
            except (json.JSONDecodeError, TypeError):
                continue
    return entries


def replay_dead_letter(activity_id: int) -> bool:
    """Re-queue a dead letter for execution.

    Resets the activity status to PENDING and clears the dead-letter
    entry. Returns True if the replay was registered, False if the
    activity was not found in the dead-letter store.
    """
    from activities.models import Activity
    from activities.services import ActivityStateMachine

    key = _dl_key(activity_id)
    raw = cache.get(key)
    if raw is None:
        return False

    try:
        activity = Activity.objects.get(id=activity_id)
        activity.last_error = None
        activity.retry_count = 0
        activity.execution_id = None
        activity.execution_started_at = None
        activity.execution_completed_at = None
        activity.save(
            update_fields=[
                "last_error",
                "retry_count",
                "execution_id",
                "execution_started_at",
                "execution_completed_at",
                "updated_at",
            ]
        )
        ActivityStateMachine.transition(activity, Activity.Status.PENDING)
    except Activity.DoesNotExist:
        # Activity was cleaned up; just remove the dead letter
        pass

    cache.delete(key)
    _remove_from_index(activity_id)

    logger.info("dead_letter_replayed activity_id=%d", activity_id)
    return True


def _remove_from_index(activity_id: int) -> None:
    index: list[int] = cache.get(DEAD_LETTER_INDEX_KEY, [])
    if activity_id in index:
        index.remove(activity_id)
        cache.set(DEAD_LETTER_INDEX_KEY, index, timeout=DEAD_LETTER_TTL)


def count_dead_letters() -> int:
    """Return the total number of dead-letter entries."""
    return len(cache.get(DEAD_LETTER_INDEX_KEY, []))


def clear_all_dead_letters() -> int:
    """Wipe all dead-letter entries. Returns the number cleared."""
    index: list[int] = cache.get(DEAD_LETTER_INDEX_KEY, [])
    count = len(index)
    for aid in index:
        cache.delete(_dl_key(aid))
    cache.delete(DEAD_LETTER_INDEX_KEY)
    return count
