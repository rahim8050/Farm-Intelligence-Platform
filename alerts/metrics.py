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
