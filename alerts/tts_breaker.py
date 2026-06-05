"""Circuit breaker for the TTS backends.

The production-readiness review (``prompts/p4-staff-engineer-review.md``
#7) flagged two TTS reliability issues:

1. A slow or hung ``piper`` / ``espeak`` invocation blocks every other
   alert because :mod:`alerts.tts` uses a process-global ``threading.Lock``.
2. When the underlying binary (e.g. ``piper``) is unhealthy, the same
   error keeps repeating for every alert with no fast-fail.

This module provides a small, dependency-free circuit breaker that
addresses both:

- A per-engine :class:`ThreadPoolExecutor` caps the number of
  in-flight TTS calls (default 4) and ensures one slow backend does
  not stall the others.
- A :class:`TTSCircuitBreaker` per engine tracks recent failures and
  short-circuits to the sine generator after ``failure_threshold``
  consecutive failures inside ``failure_window_s`` seconds. The
  breaker half-opens after ``open_for_s`` seconds and one probe
  call decides whether to close it again.

The breakers are exposed as :data:`breakers` so the Prometheus
gauge ``alerts_tts_circuit_state{engine}`` (see :mod:`alerts.metrics`)
can be refreshed in one place.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, TypeVar

from . import metrics

logger = logging.getLogger("alerts.tts")

T = TypeVar("T")


@dataclass
class _BreakerConfig:
    """Tunables for the breaker."""

    failure_threshold: int = 5
    failure_window_s: float = 60.0
    open_for_s: float = 60.0


class TTSCircuitOpenError(Exception):
    """Raised when a TTS call is short-circuited by the breaker."""


class TTSCircuitBreaker:
    """Per-engine circuit breaker with a half-open probe."""

    def __init__(
        self,
        *,
        engine: str,
        config: _BreakerConfig | None = None,
    ) -> None:
        self.engine = engine
        self._cfg = config or _BreakerConfig()
        self._lock = threading.Lock()
        self._failures: list[float] = []
        self._opened_at: float | None = None
        self._publish_state()

    def _publish_state(self) -> None:
        try:
            metrics.tts_circuit_state(
                engine=self.engine, open_=self._opened_at is not None
            )
        except Exception:  # noqa: BLE001 - metric publishing must never raise
            logger.debug(
                "tts_circuit_state publish failed for %s", self.engine
            )

    @property
    def is_open(self) -> bool:
        with self._lock:
            if self._opened_at is None:
                return False
            if time.monotonic() - self._opened_at >= self._cfg.open_for_s:
                # Half-open: allow one probe through.
                return False
            return True

    def _record_success(self) -> None:
        with self._lock:
            self._failures.clear()
            was_open = self._opened_at is not None
            self._opened_at = None
        if was_open:
            logger.info(
                "TTS circuit %s closed after probe success", self.engine
            )
            self._publish_state()

    def _record_failure(self) -> None:
        now = time.monotonic()
        with self._lock:
            self._failures.append(now)
            cutoff = now - self._cfg.failure_window_s
            self._failures = [t for t in self._failures if t >= cutoff]
            if (
                len(self._failures) >= self._cfg.failure_threshold
                and self._opened_at is None
            ):
                self._opened_at = now
                logger.warning(
                    "TTS circuit %s opened after %d failures in %.0fs",
                    self.engine,
                    len(self._failures),
                    self._cfg.failure_window_s,
                )
                self._publish_state()

    def call(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Run ``fn`` if the breaker is closed; raise
        :class:`TTSCircuitOpenError` if it is open.

        The caller is expected to catch :class:`TTSCircuitOpenError`
        and fall back to the sine generator (mirrors the original
        behaviour where every backend exception degraded to sine).
        """
        if self.is_open:
            raise TTSCircuitOpenError(self.engine)
        try:
            result = fn(*args, **kwargs)
        except Exception:
            self._record_failure()
            raise
        self._record_success()
        return result


@dataclass
class TTSExecutorPool:
    """Per-engine executor + breaker pair.

    The dispatcher in :mod:`alerts.tts` calls
    :meth:`submit` with the backend function. The pool submits the
    call to its :class:`ThreadPoolExecutor`, wraps the future with
    the breaker (success closes, failure opens), and returns the
    :class:`concurrent.futures.Future` so the caller can wait for
    the result.
    """

    engine: str
    executor: ThreadPoolExecutor
    breaker: TTSCircuitBreaker
    max_workers: int


_breakers_lock = threading.RLock()
_breakers: dict[str, TTSCircuitBreaker] = {}
_pools: dict[str, TTSExecutorPool] = {}


def get_breaker(engine: str) -> TTSCircuitBreaker:
    """Return (creating if needed) the breaker for ``engine``."""
    with _breakers_lock:
        b = _breakers.get(engine)
        if b is None:
            b = TTSCircuitBreaker(engine=engine)
            _breakers[engine] = b
        return b


def get_pool(engine: str, *, max_workers: int) -> TTSExecutorPool:
    """Return (creating if needed) the executor pool for ``engine``.

    ``max_workers`` is honoured only on first call for that engine;
    later calls with a different value are ignored so the executor
    is not torn down under a Celery worker.
    """
    with _breakers_lock:
        pool = _pools.get(engine)
        if pool is None:
            executor = ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix=f"tts-{engine}",
            )
            pool = TTSExecutorPool(
                engine=engine,
                executor=executor,
                breaker=get_breaker(engine),
                max_workers=max_workers,
            )
            _pools[engine] = pool
        return pool


def reset_breakers() -> None:
    """Reset all breakers and shut down all pools.

    Intended for tests; production code should never call this.
    Uses ``wait=True`` (the default) so the calling thread waits
    for any in-flight TTS calls to finish before the executor is
    collected; pytest teardown otherwise races with
    ``ThreadPoolExecutor.__del__`` and deadlocks.
    """
    with _breakers_lock:
        for pool in _pools.values():
            pool.executor.shutdown(wait=True)
        _pools.clear()
        _breakers.clear()


def probe_engines() -> dict[str, bool]:
    """Return a mapping ``engine -> is_open`` for the current state.

    Used by :mod:`alerts.apps` at startup to publish the initial
    Prometheus gauge value (the gauge is per-engine, so the SLO
    rule ``AlertsTTSCircuitOpen`` can fire immediately if a breaker
    was already open from a previous run).
    """
    with _breakers_lock:
        return {name: b.is_open for name, b in _breakers.items()}


def submit(  # pragma: no cover - retained for future use  # noqa: UP047
    engine: str,
    fn: Callable[..., T],
    *args: Any,
    max_workers: int = 4,
    **kwargs: Any,
) -> Future[T]:
    """Submit ``fn`` to the per-engine executor.

    The breaker state is checked at submit time and after the
    future resolves. Failures call :meth:`TTSCircuitBreaker._record_failure`
    automatically. Kept for callers that prefer a future-based
    interface; the hot path in :mod:`alerts.tts` uses
    :func:`alerts.tts._synth_via_executor` directly.
    """
    pool = get_pool(engine, max_workers=max_workers)
    if pool.breaker.is_open:
        raise TTSCircuitOpenError(engine)

    def _wrapped() -> T:
        return pool.breaker.call(fn, *args, **kwargs)

    return pool.executor.submit(_wrapped)
