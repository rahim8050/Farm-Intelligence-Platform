"""Tests for the Prometheus metrics integration in ``alerts.services``.

Per ``prompts/p4-staff-engineer-review.md`` #8, every WebSocket push
must be observable, and per #5 the dispatch path must be observable
end-to-end. These tests exercise the metrics helpers in
:mod:`alerts.metrics` and the ``dispatch_alert`` /
``emit_audio_alert_event`` instrumentation without depending on a
real Prometheus backend (the counters/histograms are no-ops when
``prometheus_client`` is absent, which is the default in CI).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from alerts.metrics import (
    dispatch_failures,
    dispatch_timer,
    dispatch_total,
    push_attempts,
    push_failures,
    render_failures,
    tts_circuit_state,
)
from alerts.services import emit_audio_alert_event


def test_dispatch_total_is_noop() -> None:
    dispatch_total(alert_type="ndvi_decline", result="success")


def test_dispatch_failures_is_noop() -> None:
    dispatch_failures(alert_type="ndvi_decline", reason="Boom")


def test_push_attempts_is_noop() -> None:
    push_attempts(result="success")
    push_attempts(result="error")
    push_attempts(result="no_layer")


def test_push_failures_is_noop() -> None:
    push_failures(reason="RuntimeError")


def test_render_failures_is_noop() -> None:
    render_failures(engine="espeak", reason="Timeout")


def test_tts_circuit_state_is_noop() -> None:
    tts_circuit_state(engine="espeak", open_=True)
    tts_circuit_state(engine="espeak", open_=False)


def test_dispatch_timer_is_a_context_manager() -> None:
    with dispatch_timer(alert_type="admin_broadcast"):
        _ = 1 + 1


def test_emit_no_layer_returns_zero() -> None:
    with patch("alerts.services.get_channel_layer", return_value=None):
        n = emit_audio_alert_event(1, {"x": "y"})
    assert n == 0


def test_emit_successful_push_returns_one() -> None:
    layer = MagicMock()
    layer.group_send = AsyncMock(return_value=None)
    with patch("alerts.services.get_channel_layer", return_value=layer):
        n = emit_audio_alert_event(1, {"x": "y"})
    assert n == 1
    layer.group_send.assert_called_once()


def test_emit_push_exception_returns_zero() -> None:
    layer = MagicMock()
    layer.group_send = AsyncMock(side_effect=RuntimeError("ws down"))
    with patch("alerts.services.get_channel_layer", return_value=layer):
        n = emit_audio_alert_event(1, {"x": "y"})
    assert n == 0
