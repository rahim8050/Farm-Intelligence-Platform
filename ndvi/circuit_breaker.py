"""Generic circuit breaker for upstream service protection.

Provides circuit breaker pattern for all data providers (STAC, SentinelHub,
GEE, Landsat, MODIS) with Prometheus metrics integration.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from ndvi.metrics import (
    ndvi_circuit_breaker_state,
    ndvi_circuit_breaker_transitions_total,
    spectral_provider_circuit_state,
)

logger = logging.getLogger(__name__)


# State to numeric value mapping for Prometheus gauge
_STATE_VALUES: dict[str, int] = {
    "closed": 0,
    "open": 1,
    "half_open": 2,
}


class CircuitOpenError(RuntimeError):
    """Raised when a request is blocked by an open circuit breaker."""

    def __init__(
        self,
        *,
        engine: str,
        timeout_secs: float,
        elapsed: float,
    ) -> None:
        self.engine = engine
        self.timeout_secs = timeout_secs
        self.elapsed = elapsed
        remaining = max(timeout_secs - elapsed, 0)
        super().__init__(
            f"Circuit breaker for '{engine}' is open. "
            f"Upstream has been unreachable for {elapsed:.0f}s. "
            f"Will attempt recovery in {remaining:.0f}s."
        )


@dataclass(frozen=True)
class ProviderHealth:
    """Health status for a data provider.

    Attributes:
        provider: Provider name (e.g. ``"stac"``, ``"sentinelhub"``).
        state: Circuit breaker state (``"closed"``, ``"open"``,
            ``"half_open"``).
        failure_count: Current consecutive failure count.
        failure_threshold: Number of failures before circuit opens.
        seconds_since_last_failure: Time since last failure.
        is_healthy: ``True`` if state is ``CLOSED`` or ``HALF_OPEN``.
    """

    provider: str
    state: str
    failure_count: int
    failure_threshold: int
    seconds_since_last_failure: float
    is_healthy: bool


class CircuitBreaker:
    """Generic circuit breaker to stop retrying when an upstream is blocked.

    States:
      - CLOSED: Normal operation, requests pass through.
      - OPEN: Circuit is tripped, all requests fail immediately.
      - HALF_OPEN: Testing if the upstream has recovered.

    The circuit opens after `failure_threshold` consecutive failures
    and closes again after one successful request in HALF_OPEN state.

    Usage:
        cb = CircuitBreaker(
            engine="stac",
            failure_threshold=3,
            reset_timeout_secs=300.0,
        )

        # Using call() wrapper:
        result = cb.call(make_request, arg1, arg2)

        # Or manually:
        if cb.is_open():
            raise CircuitOpenError(...)

        try:
            response = make_request()
            cb.record_success()
        except Exception:
            cb.record_failure()
            raise
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(
        self,
        *,
        engine: str,
        failure_threshold: int = 3,
        reset_timeout_secs: float = 300.0,
    ) -> None:
        self.engine = engine
        self._failure_threshold = failure_threshold
        self._reset_timeout_secs = reset_timeout_secs
        self._state = self.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0

        # Initialize Prometheus gauges to 0 (CLOSED)
        ndvi_circuit_breaker_state.labels(engine=engine).set(
            _STATE_VALUES[self.CLOSED]
        )
        spectral_provider_circuit_state.labels(provider=engine).set(
            _STATE_VALUES[self.CLOSED]
        )

    def _record_transition(self, from_state: str, to_state: str) -> None:
        """Record a state transition in Prometheus metrics."""
        ndvi_circuit_breaker_state.labels(engine=self.engine).set(
            _STATE_VALUES[to_state]
        )
        ndvi_circuit_breaker_transitions_total.labels(
            engine=self.engine,
            from_state=from_state,
            to_state=to_state,
        ).inc()
        spectral_provider_circuit_state.labels(provider=self.engine).set(
            _STATE_VALUES[to_state]
        )

    @property
    def state(self) -> str:
        """Return current state, auto-transitioning OPEN→HALF_OPEN on
        timeout."""
        if self._state == self.OPEN:
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed >= self._reset_timeout_secs:
                old_state = self._state
                self._state = self.HALF_OPEN
                self._record_transition(old_state, self.HALF_OPEN)
                logger.info(
                    "ndvi.circuit_breaker engine=%s"
                    " OPEN→HALF_OPEN after %.0fs",
                    self.engine,
                    elapsed,
                )
        return self._state

    def record_success(self) -> None:
        """Record a successful request."""
        self._failure_count = 0
        if self._state == self.HALF_OPEN:
            old_state = self._state
            self._state = self.CLOSED
            self._record_transition(old_state, self.CLOSED)
            logger.info(
                "ndvi.circuit_breaker engine=%s %s→CLOSED (recovered)",
                self.engine,
                old_state,
            )

    def record_failure(self) -> None:
        """Record a failed request."""
        self._failure_count += 1
        self._last_failure_time = time.monotonic()

        if self._state == self.HALF_OPEN:
            old_state = self._state
            self._state = self.OPEN
            self._record_transition(old_state, self.OPEN)
            logger.warning(
                "ndvi.circuit_breaker engine=%s %s→OPEN (upstream still down)",
                self.engine,
                old_state,
            )
        elif self._failure_count >= self._failure_threshold:
            old_state = self._state
            self._state = self.OPEN
            self._record_transition(old_state, self.OPEN)
            logger.warning(
                "ndvi.circuit_breaker engine=%s %s→OPEN after %d failures",
                self.engine,
                old_state,
                self._failure_count,
            )

    def is_open(self) -> bool:
        """Check if the circuit is open (should block requests)."""
        return self.state == self.OPEN

    def call(
        self,
        func: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Execute a function through the circuit breaker.

        If the circuit is OPEN, raises ``CircuitOpenError`` immediately.
        If HALF_OPEN, allows the call as a probe. On success, closes the
        circuit. On failure, re-opens the circuit.

        Args:
            func: Callable to execute.
            *args: Positional arguments for func.
            **kwargs: Keyword arguments for func.

        Returns:
            The return value of func.

        Raises:
            CircuitOpenError: If the circuit is OPEN.
            Any exception raised by func is propagated.
        """
        if self.is_open():
            elapsed = 0.0
            if self._last_failure_time > 0:
                elapsed = time.monotonic() - self._last_failure_time
            raise CircuitOpenError(
                engine=self.engine,
                timeout_secs=self._reset_timeout_secs,
                elapsed=elapsed,
            )

        try:
            result = func(*args, **kwargs)
            self.record_success()
            return result
        except Exception:
            self.record_failure()
            raise

    def reset(self) -> None:
        """Manually reset the circuit breaker to CLOSED state."""
        old_state = self._state
        self._state = self.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0
        if old_state != self.CLOSED:
            self._record_transition(old_state, self.CLOSED)
        logger.info(
            "ndvi.circuit_breaker engine=%s %s→CLOSED (manual reset)",
            self.engine,
            old_state,
        )

    def get_status(self) -> dict[str, Any]:
        """Return a status dict for observability."""
        current_state = self.state  # Triggers OPEN→HALF_OPEN check
        elapsed = 0.0
        if self._last_failure_time > 0:
            elapsed = time.monotonic() - self._last_failure_time
        return {
            "engine": self.engine,
            "state": current_state,
            "failure_count": self._failure_count,
            "failure_threshold": self._failure_threshold,
            "reset_timeout_secs": self._reset_timeout_secs,
            "seconds_since_last_failure": elapsed,
        }

    def health(self) -> ProviderHealth:
        """Return a structured health report for this circuit breaker.

        Returns:
            ``ProviderHealth`` with current state and health assessment.
        """
        current_state = self.state
        elapsed = 0.0
        if self._last_failure_time > 0:
            elapsed = time.monotonic() - self._last_failure_time
        return ProviderHealth(
            provider=self.engine,
            state=current_state,
            failure_count=self._failure_count,
            failure_threshold=self._failure_threshold,
            seconds_since_last_failure=elapsed,
            is_healthy=current_state != self.OPEN,
        )


# ---------------------------------------------------------------------------
# Registry for tracking circuit breaker instances by engine name
# ---------------------------------------------------------------------------

_ENGINE_REGISTRY: dict[str, CircuitBreaker] = {}


def register_circuit_breaker(cb: CircuitBreaker) -> None:
    """Register a circuit breaker instance in the global registry."""
    _ENGINE_REGISTRY[cb.engine] = cb


def get_circuit_breaker(engine: str) -> CircuitBreaker | None:
    """Look up a circuit breaker by engine name."""
    return _ENGINE_REGISTRY.get(engine)


def list_circuit_breakers() -> dict[str, CircuitBreaker]:
    """Return a copy of the full registry (for health checks)."""
    return dict(_ENGINE_REGISTRY)
