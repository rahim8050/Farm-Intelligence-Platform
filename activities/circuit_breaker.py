"""Circuit breaker for activity handler execution.

Tracks consecutive handler failures per type and temporarily
disables dispatch when a threshold is exceeded. Cache-backed
so it works across multiple worker processes.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from django.core.cache import cache

from activities.metrics import (
    activities_circuit_breaker_resets,
    activities_circuit_breaker_trips,
)

logger = logging.getLogger("activities")

CB_PREFIX = "activities:cb:"
CB_HALF_OPEN_PREFIX = "activities:cb:half_open:"
CB_COUNTER_PREFIX = "activities:cb:counter:"

CB_FAILURE_THRESHOLD = 5
CB_RESET_TIMEOUT = 300
CB_HALF_OPEN_MAX = 1


def _cb_key(handler_type: str) -> str:
    return f"{CB_PREFIX}{handler_type}"


def _counter_key(handler_type: str) -> str:
    return f"{CB_COUNTER_PREFIX}{handler_type}"


def _half_open_key(handler_type: str) -> str:
    return f"{CB_HALF_OPEN_PREFIX}{handler_type}"


def record_failure(handler_type: str) -> None:
    """Record a handler failure and trip the breaker if threshold exceeded."""
    counter_key = _counter_key(handler_type)
    failures = cache.get(counter_key, 0) + 1
    cache.set(counter_key, failures, timeout=CB_RESET_TIMEOUT)

    if failures >= CB_FAILURE_THRESHOLD:
        cb_key = _cb_key(handler_type)
        cache.set(cb_key, time.time(), timeout=CB_RESET_TIMEOUT)
        cache.delete(counter_key)
        activities_circuit_breaker_trips.labels(type=handler_type).inc()
        logger.warning(
            "circuit_breaker_tripped handler_type=%s failures=%d "
            "reset_timeout=%ds",
            handler_type,
            failures,
            CB_RESET_TIMEOUT,
        )


def record_success(handler_type: str) -> None:
    """Reset the failure counter on success (close the breaker)."""
    cache.delete(_counter_key(handler_type))
    cb_key = _cb_key(handler_type)
    if cache.get(cb_key) is not None:
        cache.delete(cb_key)
        activities_circuit_breaker_resets.labels(type=handler_type).inc()
        logger.info("circuit_breaker_reset handler_type=%s", handler_type)


def is_open(handler_type: str) -> bool:
    """Check if the circuit breaker is open (dispatch should be blocked)."""
    return cache.get(_cb_key(handler_type)) is not None


def can_try_half_open(handler_type: str) -> bool:
    """Allow a single probe request when breaker is open.

    Returns True if this caller should be allowed to try.
    """
    key = _half_open_key(handler_type)
    if cache.add(key, "1", timeout=CB_RESET_TIMEOUT):
        return True
    return False


def get_breaker_state(handler_type: str) -> dict[str, Any]:
    """Return the current breaker state for diagnostics."""
    open_ts = cache.get(_cb_key(handler_type))
    failures = cache.get(_counter_key(handler_type), 0)
    return {
        "handler_type": handler_type,
        "state": "open"
        if open_ts
        else (
            "half_open"
            if cache.get(_half_open_key(handler_type))
            else "closed"
        ),
        "since": open_ts,
        "failures": failures,
        "threshold": CB_FAILURE_THRESHOLD,
        "reset_timeout": CB_RESET_TIMEOUT,
    }
