"""Generic circuit breaker for upstream service protection."""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


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

    @property
    def state(self) -> str:
        """Return current state, auto-transitioning OPEN→HALF_OPEN on
        timeout."""
        if self._state == self.OPEN:
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed >= self._reset_timeout_secs:
                self._state = self.HALF_OPEN
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
            logger.warning(
                "ndvi.circuit_breaker engine=%s %s→OPEN (upstream still down)",
                self.engine,
                old_state,
            )
        elif self._failure_count >= self._failure_threshold:
            old_state = self._state
            self._state = self.OPEN
            logger.warning(
                "ndvi.circuit_breaker engine=%s %s→OPEN after %d failures",
                self.engine,
                old_state,
                self._failure_count,
            )

    def is_open(self) -> bool:
        """Check if the circuit is open (should block requests)."""
        return self.state == self.OPEN

    def reset(self) -> None:
        """Manually reset the circuit breaker to CLOSED state."""
        old_state = self._state
        self._state = self.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0
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
