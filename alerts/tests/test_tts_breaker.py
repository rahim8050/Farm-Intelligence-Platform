"""Tests for the TTS circuit breaker.

Per ``prompts/p4-staff-engineer-review.md`` #7:

- A per-engine :class:`TTSCircuitBreaker` should open after
  ``failure_threshold`` failures inside ``failure_window_s`` and
  half-open after ``open_for_s``.
- Successes close the breaker; the next call goes through.
- The :func:`probe_engines` helper returns the current state per
  engine so :mod:`alerts.apps` can publish it on startup.
"""

from __future__ import annotations

import time
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor

import pytest

from alerts.tts_breaker import (
    TTSCircuitBreaker,
    TTSCircuitOpenError,
    _BreakerConfig,
    get_breaker,
    get_pool,
    probe_engines,
    reset_breakers,
)


@pytest.fixture(autouse=True)
def _clean_breakers() -> Generator[None, None, None]:
    reset_breakers()
    yield
    reset_breakers()


def _fast_config(
    *, threshold: int = 2, window_s: float = 1.0, open_for_s: float = 1.0
) -> _BreakerConfig:
    return _BreakerConfig(
        failure_threshold=threshold,
        failure_window_s=window_s,
        open_for_s=open_for_s,
    )


def _raise(exc: Exception) -> None:
    raise exc


def test_breaker_starts_closed() -> None:
    b = TTSCircuitBreaker(engine="noop", config=_fast_config(threshold=1))
    assert b.is_open is False


def test_breaker_success_does_not_open() -> None:
    b = TTSCircuitBreaker(engine="noop", config=_fast_config(threshold=3))
    for _ in range(10):
        assert b.call(lambda: "ok") == "ok"
    assert b.is_open is False


def test_breaker_opens_after_threshold() -> None:
    b = TTSCircuitBreaker(engine="espeak", config=_fast_config(threshold=2))
    with pytest.raises(RuntimeError):
        b.call(_raise, RuntimeError("boom"))
    assert b.is_open is False
    with pytest.raises(RuntimeError):
        b.call(_raise, RuntimeError("boom"))
    assert b.is_open is True


def test_breaker_open_raises_circuit_error() -> None:
    b = TTSCircuitBreaker(engine="piper", config=_fast_config(threshold=1))
    with pytest.raises(RuntimeError):
        b.call(_raise, RuntimeError("boom"))
    with pytest.raises(TTSCircuitOpenError):
        b.call(lambda: "should not run")


def test_breaker_success_closes() -> None:
    b = TTSCircuitBreaker(engine="noop", config=_fast_config(threshold=1))
    with pytest.raises(RuntimeError):
        b.call(_raise, RuntimeError("boom"))
    assert b.is_open is True
    b._record_success()  # noqa: SLF001 - manual reset for the test
    assert b.is_open is False


def test_breaker_half_open_after_open_for_s() -> None:
    b = TTSCircuitBreaker(
        engine="noop",
        config=_fast_config(threshold=1, open_for_s=0.05),
    )
    with pytest.raises(RuntimeError):
        b.call(_raise, RuntimeError("boom"))
    assert b.is_open is True
    time.sleep(0.1)
    # half-open: next call goes through
    assert b.call(lambda: "ok") == "ok"
    assert b.is_open is False


def test_pool_returns_same_instance() -> None:
    a = get_pool("noop", max_workers=2)
    b = get_pool("noop", max_workers=4)  # ignored
    assert a is b
    assert a.max_workers == 2


def test_pool_uses_threadpool() -> None:
    pool = get_pool("noop", max_workers=2)
    assert isinstance(pool.executor, ThreadPoolExecutor)
    fut = pool.executor.submit(lambda: 1)
    assert fut.result(timeout=2) == 1


def test_get_breaker_returns_same_instance() -> None:
    a = get_breaker("noop")
    b = get_breaker("noop")
    assert a is b


def test_different_engines_different_breakers() -> None:
    a = get_breaker("piper")
    b = get_breaker("espeak")
    assert a is not b


def test_probe_engines_empty_initially() -> None:
    # No breakers have been created yet.
    assert probe_engines() == {}


def test_probe_engines_returns_current_state() -> None:
    b = get_breaker("noop")
    b._record_failure()  # noqa: SLF001
    # single failure isn't enough to open
    assert probe_engines() == {"noop": False}
