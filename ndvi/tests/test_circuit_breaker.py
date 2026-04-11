"""Tests for ndvi.circuit_breaker module."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest
from prometheus_client import REGISTRY

from ndvi.circuit_breaker import (
    _STATE_VALUES,
    CircuitBreaker,
    CircuitOpenError,
)


@pytest.fixture
def cb() -> CircuitBreaker:
    """Create a circuit breaker with fast timeouts for testing."""
    return CircuitBreaker(
        engine="test_engine",
        failure_threshold=3,
        reset_timeout_secs=1.0,
    )


# --- State transitions ---


def test_initial_state_is_closed(cb: CircuitBreaker) -> None:
    assert cb.state == CircuitBreaker.CLOSED
    assert cb.is_open() is False


def test_opens_after_threshold_failures(cb: CircuitBreaker) -> None:
    for _ in range(3):
        cb.record_failure()
    assert cb.state == CircuitBreaker.OPEN
    assert cb.is_open() is True


def test_half_open_after_timeout(cb: CircuitBreaker) -> None:
    # Trip the circuit
    for _ in range(3):
        cb.record_failure()
    assert cb.state == CircuitBreaker.OPEN

    # Advance time past the reset timeout
    with patch.object(time, "monotonic", return_value=time.monotonic() + 2.0):
        assert cb.state == CircuitBreaker.HALF_OPEN
        assert cb.is_open() is False


def test_half_open_to_closed_on_success(cb: CircuitBreaker) -> None:
    # Trip and transition to HALF_OPEN
    for _ in range(3):
        cb.record_failure()
    with patch.object(time, "monotonic", return_value=time.monotonic() + 2.0):
        assert cb.state == CircuitBreaker.HALF_OPEN

    # Success should close the circuit
    cb.record_success()
    assert cb.state == CircuitBreaker.CLOSED
    assert cb.is_open() is False


def test_half_open_to_open_on_failure(cb: CircuitBreaker) -> None:
    # Trip and transition to HALF_OPEN
    for _ in range(3):
        cb.record_failure()
    with patch.object(time, "monotonic", return_value=time.monotonic() + 2.0):
        assert cb.state == CircuitBreaker.HALF_OPEN

    # Failure in HALF_OPEN should re-open
    cb.record_failure()
    assert cb.state == CircuitBreaker.OPEN
    assert cb.is_open() is True


def test_failure_count_resets_on_success(cb: CircuitBreaker) -> None:
    cb.record_failure()
    cb.record_failure()
    assert cb._failure_count == 2

    cb.record_success()
    assert cb._failure_count == 0

    # Two more failures should NOT trip the circuit yet
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitBreaker.CLOSED


# --- Manual reset ---


def test_manual_reset_clears_state(cb: CircuitBreaker) -> None:
    for _ in range(3):
        cb.record_failure()
    assert cb.state == CircuitBreaker.OPEN

    cb.reset()
    assert cb.state == CircuitBreaker.CLOSED
    assert cb.is_open() is False
    assert cb._failure_count == 0


# --- Status reporting ---


def test_get_status_closed(cb: CircuitBreaker) -> None:
    status = cb.get_status()
    assert status["engine"] == "test_engine"
    assert status["state"] == "closed"
    assert status["failure_count"] == 0
    assert status["failure_threshold"] == 3


def test_get_status_open(cb: CircuitBreaker) -> None:
    for _ in range(3):
        cb.record_failure()
    status = cb.get_status()
    assert status["state"] == "open"
    assert status["failure_count"] == 3
    assert status["seconds_since_last_failure"] > 0


# --- CircuitOpenError ---


def test_circuit_open_error_message() -> None:
    err = CircuitOpenError(
        engine="stac",
        timeout_secs=300.0,
        elapsed=60.0,
    )
    assert "stac" in str(err)
    assert "240" in str(err)
    assert isinstance(err, RuntimeError)


# --- Edge cases ---


def test_single_failure_does_not_open(cb: CircuitBreaker) -> None:
    cb.record_failure()
    assert cb.state == CircuitBreaker.CLOSED


def test_one_less_than_threshold_does_not_open(cb: CircuitBreaker) -> None:
    for _ in range(2):
        cb.record_failure()
    assert cb.state == CircuitBreaker.CLOSED


def test_exact_threshold_opens(cb: CircuitBreaker) -> None:
    for _ in range(3):
        cb.record_failure()
    assert cb.state == CircuitBreaker.OPEN


def test_many_failures_keeps_open(cb: CircuitBreaker) -> None:
    for _ in range(10):
        cb.record_failure()
    assert cb.state == CircuitBreaker.OPEN


def test_success_in_closed_state_is_noop(cb: CircuitBreaker) -> None:
    cb.record_success()
    cb.record_success()
    assert cb.state == CircuitBreaker.CLOSED
    assert cb._failure_count == 0


# --- Prometheus metrics integration ---


def _get_metric_value(name: str, engine: str) -> float:
    """Helper to read a specific metric value from Prometheus registry."""
    samples = REGISTRY.get_sample_value(
        name,
        {"engine": engine},
    )
    return samples  # type: ignore[return-value]


def _get_transition_count(
    engine: str,
    from_state: str,
    to_state: str,
) -> float:
    """Helper to read transition counter value."""
    samples = REGISTRY.get_sample_value(
        "ndvi_circuit_breaker_transitions_total",
        {
            "engine": engine,
            "from_state": from_state,
            "to_state": to_state,
        },
    )
    return samples or 0.0  # type: ignore[return-value]


def test_initial_state_sets_gauge_to_zero(cb: CircuitBreaker) -> None:
    """New circuit breaker should set gauge to 0 (CLOSED)."""
    value = _get_metric_value("ndvi_circuit_breaker_state", cb.engine)
    assert value == _STATE_VALUES[CircuitBreaker.CLOSED]


def test_record_failure_trips_circuit_updates_gauge(
    cb: CircuitBreaker,
) -> None:
    """Opening circuit should update gauge to 1 (OPEN)."""
    for _ in range(3):
        cb.record_failure()

    value = _get_metric_value("ndvi_circuit_breaker_state", cb.engine)
    assert value == _STATE_VALUES[CircuitBreaker.OPEN]


def test_transitions_are_counted(cb: CircuitBreaker) -> None:
    """State transitions should increment the transition counter."""
    # Trip the circuit (CLOSED→OPEN)
    before = _get_transition_count(cb.engine, "closed", "open")
    for _ in range(3):
        cb.record_failure()
    after = _get_transition_count(cb.engine, "closed", "open")
    assert after - before == 1.0


def test_half_open_transition_counts(cb: CircuitBreaker) -> None:
    """OPEN→HALF_OPEN transition should be counted."""
    # Trip the circuit
    for _ in range(3):
        cb.record_failure()

    # Advance time to trigger HALF_OPEN
    before = _get_transition_count(cb.engine, "open", "half_open")
    with patch.object(time, "monotonic", return_value=time.monotonic() + 2.0):
        _ = cb.state  # Triggers OPEN→HALF_OPEN check

    after = _get_transition_count(cb.engine, "open", "half_open")
    assert after - before == 1.0


def test_reset_updates_metrics(cb: CircuitBreaker) -> None:
    """Manual reset should update gauge to CLOSED."""
    # Trip the circuit
    for _ in range(3):
        cb.record_failure()

    cb.reset()
    value = _get_metric_value("ndvi_circuit_breaker_state", cb.engine)
    assert value == _STATE_VALUES[CircuitBreaker.CLOSED]
