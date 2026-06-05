"""Prometheus metrics for the ``alerts`` app.

These cover the production-readiness review in
``prompts/p4-staff-engineer-review.md`` (#5, #6, #7, #8): counters
and histograms needed to define SLOs and Prometheus alerting
rules for the alert dispatch pipeline.

Exposed series:

- :data:`alerts_dispatch_total` - incremented once per
  :func:`alerts.services.dispatch_alert` call, labeled by
  ``alert_type`` and ``result`` (``ok`` / ``fail``).
- :data:`alerts_dispatch_failures_total` - incremented when
  ``dispatch_alert`` raises or the WebSocket push fails, labeled
  by ``reason``.
- :data:`alerts_dispatch_duration_seconds` - histogram of the
  full dispatch latency (DB write + TTS + push).
- :data:`alerts_render_duration_seconds` - histogram of TTS
  render-only time (see :func:`alerts.tasks.render_alert_audio`).
- :data:`alerts_render_failures_total` - incremented when the
  TTS backend raises, labeled by ``engine``.
- :data:`alerts_push_attempts_total` / ``alerts_push_failures_total`` -
  labeled by ``result`` and ``reason`` (see #8).
- :data:`alerts_tts_circuit_state` - gauge (0=closed, 1=open) per
  TTS engine.

All metrics are no-ops if ``prometheus_client`` is not installed.
"""

from __future__ import annotations

import contextlib
from typing import Any

try:  # pragma: no cover - exercised in production
    from prometheus_client import Counter, Gauge, Histogram
except Exception:  # pragma: no cover - dev / test fallback
    Counter = Gauge = Histogram = None  # type: ignore[assignment,misc]


def _counter(name: str, doc: str, labels: list[str]) -> Any:
    if Counter is None:  # pragma: no cover
        return None
    return Counter(name, doc, labels)


def _histogram(name: str, doc: str, labels: list[str]) -> Any:
    if Histogram is None:  # pragma: no cover
        return None
    return Histogram(name, doc, labels)


def _gauge(name: str, doc: str, labels: list[str]) -> Any:
    if Gauge is None:  # pragma: no cover
        return None
    return Gauge(name, doc, labels)


# --- Dispatch -----------------------------------------------------------

alerts_dispatch_total = _counter(
    "alerts_dispatch_total",
    "AudioAlert rows created via dispatch_alert.",
    ["alert_type", "result"],
)
alerts_dispatch_failures_total = _counter(
    "alerts_dispatch_failures_total",
    "dispatch_alert failures (DB write, TTS, or push).",
    ["alert_type", "reason"],
)
alerts_dispatch_duration_seconds = _histogram(
    "alerts_dispatch_duration_seconds",
    "Full dispatch_alert latency (DB + TTS + push).",
    ["alert_type"],
)

# --- TTS render ---------------------------------------------------------

alerts_render_duration_seconds = _histogram(
    "alerts_render_duration_seconds",
    "TTS render-only latency (alerts.tasks.render_alert_audio).",
    ["engine"],
)
alerts_render_failures_total = _counter(
    "alerts_render_failures_total",
    "TTS render failures (after circuit breaker fast-fails).",
    ["engine", "reason"],
)
alerts_tts_circuit_state = _gauge(
    "alerts_tts_circuit_state",
    "TTS circuit-breaker state (0=closed, 1=open).",
    ["engine"],
)

# --- WebSocket push -----------------------------------------------------

alerts_push_attempts_total = _counter(
    "alerts_push_attempts_total",
    "WebSocket push attempts for audio_alert events.",
    ["result"],
)
alerts_push_failures_total = _counter(
    "alerts_push_failures_total",
    "WebSocket push failures, labeled by reason.",
    ["reason"],
)


# --- Helpers ------------------------------------------------------------
#
# Plain functions (not context managers) so call sites stay readable
# and so the no-op fallback when ``prometheus_client`` is absent does
# not require implementing ``__enter__``/``__exit__``.


def dispatch_total(*, alert_type: str, result: str) -> None:
    """Increment :data:`alerts_dispatch_total`."""
    if alerts_dispatch_total is None:  # pragma: no cover
        return
    alerts_dispatch_total.labels(alert_type=alert_type, result=result).inc()


def dispatch_failures(*, alert_type: str, reason: str) -> None:
    """Increment :data:`alerts_dispatch_failures_total`."""
    if alerts_dispatch_failures_total is None:  # pragma: no cover
        return
    alerts_dispatch_failures_total.labels(
        alert_type=alert_type, reason=reason
    ).inc()


@contextlib.contextmanager
def dispatch_timer(*, alert_type: str) -> Any:
    """Context manager that times the dispatch path."""
    if alerts_dispatch_duration_seconds is None:  # pragma: no cover
        yield
        return
    with alerts_dispatch_duration_seconds.labels(alert_type=alert_type).time():
        yield


def render_duration(*, engine: str, seconds: float) -> None:
    """Observe a single TTS render duration (seconds)."""
    if alerts_render_duration_seconds is None:  # pragma: no cover
        return
    alerts_render_duration_seconds.labels(engine=engine).observe(seconds)


def render_failures(*, engine: str, reason: str) -> None:
    """Increment :data:`alerts_render_failures_total`."""
    if alerts_render_failures_total is None:  # pragma: no cover
        return
    alerts_render_failures_total.labels(engine=engine, reason=reason).inc()


def tts_circuit_state(*, engine: str, open_: bool) -> None:
    """Set the TTS circuit-breaker gauge (0=closed, 1=open)."""
    if alerts_tts_circuit_state is None:  # pragma: no cover
        return
    alerts_tts_circuit_state.labels(engine=engine).set(1 if open_ else 0)


def push_attempts(*, result: str) -> None:
    """Increment :data:`alerts_push_attempts_total`."""
    if alerts_push_attempts_total is None:  # pragma: no cover
        return
    alerts_push_attempts_total.labels(result=result).inc()


def push_failures(*, reason: str) -> None:
    """Increment :data:`alerts_push_failures_total`."""
    if alerts_push_failures_total is None:  # pragma: no cover
        return
    alerts_push_failures_total.labels(reason=reason).inc()
