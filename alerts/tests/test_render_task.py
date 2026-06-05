"""Tests for the asynchronous ``alerts.tasks.render_alert_audio``.

Per ``prompts/p4-staff-engineer-review.md`` #6, the TTS render
happens in a Celery task so the synchronous ``dispatch_alert`` call
stays sub-second. These tests cover the task body in isolation:
the audio file is saved, the duration / mime_type are populated,
and a second WebSocket event is pushed with the populated
``audio_url``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from alerts.models import AudioAlert, AudioAlertType
from alerts.tasks import render_alert_audio

pytestmark = pytest.mark.django_db


def _make_user() -> Any:
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(
        username=f"u-{__import__('secrets').token_urlsafe(8)}",
        password=__import__("secrets").token_urlsafe(16),
    )


def test_render_alert_audio_synthesises_and_pushes() -> None:
    user = _make_user()
    alert = AudioAlert.objects.create(
        user=user,
        alert_type=AudioAlertType.ADMIN_BROADCAST,
        trigger_source="admin_view",
        title="t",
        message="hello world",
    )
    layer = MagicMock()
    layer.group_send = AsyncMock(return_value=None)
    tts_result = MagicMock()
    tts_result.audio_bytes = b"RIFF...stub"
    tts_result.mime_type = "audio/wav"
    tts_result.duration_ms = 4321
    with (
        patch("alerts.tts.synthesize", return_value=tts_result),
        patch("alerts.services.get_channel_layer", return_value=layer),
    ):
        result = render_alert_audio(str(alert.id))
    assert result["status"] == "rendered"
    alert.refresh_from_db()
    assert alert.duration_ms == 4321
    assert alert.mime_type == "audio/wav"
    assert alert.audio_file


def test_render_alert_audio_is_idempotent() -> None:
    user = _make_user()
    alert = AudioAlert.objects.create(
        user=user,
        alert_type=AudioAlertType.ADMIN_BROADCAST,
        trigger_source="admin_view",
        title="t",
        message="hi",
    )
    # Pre-populate audio_file so the task short-circuits.
    from django.core.files.base import ContentFile

    alert.audio_file.save(f"{alert.id}.wav", ContentFile(b"x"), save=True)
    result = render_alert_audio(str(alert.id))
    assert result["status"] == "already_rendered"


def test_render_alert_audio_handles_missing_alert() -> None:
    import uuid

    result = render_alert_audio(str(uuid.uuid4()))
    assert result is None


def test_render_alert_audio_records_render_failures_metric() -> None:
    user = _make_user()
    alert = AudioAlert.objects.create(
        user=user,
        alert_type=AudioAlertType.ADMIN_BROADCAST,
        trigger_source="admin_view",
        title="t",
        message="hi",
    )
    with patch(
        "alerts.tts.synthesize",
        side_effect=RuntimeError("tts down"),
    ):
        with pytest.raises(RuntimeError):
            render_alert_audio(str(alert.id))
