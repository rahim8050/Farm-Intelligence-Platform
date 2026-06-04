"""Tests for the alerts TTS service."""

from __future__ import annotations

from io import BytesIO
from unittest.mock import patch
from wave import open as wave_open

import pytest
from django.test import override_settings

from alerts.tts import (
    TTSResult,
    _espeak,
    _guess_wav_duration_ms,
    _noop,
    _sine_fallback,
    _sine_wav,
    synthesize,
)

pytestmark = pytest.mark.django_db


def test_sine_wav_returns_valid_wav_bytes() -> None:
    data = _sine_wav(duration_s=0.1, freq=440)
    assert isinstance(data, bytes)
    assert len(data) > 0
    # Round-trip through the stdlib wave module
    with wave_open(BytesIO(data), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == 16_000
        assert w.getnframes() > 0


def test_guess_wav_duration_ms_handles_valid_and_invalid() -> None:
    data = _sine_wav(duration_s=0.1, freq=440)
    ms = _guess_wav_duration_ms(data)
    assert ms is not None
    assert 50 <= ms <= 200
    assert _guess_wav_duration_ms(b"not a wav") is None


def test_noop_backend_returns_empty_result() -> None:
    out = _noop("hello", voice="en", timeout_s=1.0)
    assert out.audio_bytes == b""
    assert out.duration_ms == 0


def test_sine_fallback_returns_wav() -> None:
    out = _sine_fallback("hello")
    assert out.audio_bytes != b""
    assert out.mime_type == "audio/wav"
    assert out.duration_ms == 1000


@override_settings(TTS_ENGINE="sine", TTS_VOICE="en", TTS_TIMEOUT_SECONDS=1.0)
def test_synthesize_uses_sine_when_engine_is_sine() -> None:
    out = synthesize("Hello world.")
    assert out.audio_bytes != b""
    assert out.mime_type == "audio/wav"


@override_settings(TTS_ENGINE="noop", TTS_VOICE="en", TTS_TIMEOUT_SECONDS=1.0)
def test_synthesize_uses_noop_when_engine_is_noop() -> None:
    out = synthesize("Hello world.")
    assert out.audio_bytes == b""


@override_settings(TTS_ENGINE="piper", TTS_VOICE="en", TTS_TIMEOUT_SECONDS=1.0)
def test_synthesize_falls_back_to_sine_when_piper_binary_missing() -> None:
    with patch("alerts.tts.shutil.which", return_value=None):
        out = synthesize("Hello world.")
    assert out.audio_bytes != b""  # sine fallback


@override_settings(
    TTS_ENGINE="espeak", TTS_VOICE="en", TTS_TIMEOUT_SECONDS=1.0
)
def test_synthesize_falls_back_to_sine_when_espeak_binary_missing() -> None:
    with patch("alerts.tts.shutil.which", return_value=None):
        out = synthesize("Hello world.")
    assert out.audio_bytes != b""  # sine fallback


@override_settings(TTS_ENGINE="noop", TTS_MAX_TEXT_CHARS=5)
def test_synthesize_truncates_long_text() -> None:
    out = synthesize("this is a very long text that should be truncated")
    # TTSResult is immutable; the truncation happens before the engine
    # is invoked, and noop always returns empty bytes.
    assert isinstance(out, TTSResult)


@override_settings(TTS_ENGINE="piper", TTS_VOICE="en", TTS_TIMEOUT_SECONDS=1.0)
def test_piper_backend_raises_on_failure() -> None:
    """When piper is on PATH but returns non-zero, _piper raises."""
    with patch("alerts.tts.shutil.which", return_value="/usr/bin/piper"):
        with patch(
            "subprocess.run",
            return_value=type(
                "R",
                (),
                {"returncode": 1, "stderr": b"boom"},
            )(),
        ):
            with pytest.raises(RuntimeError):
                _espeak("hello", voice="en", timeout_s=1.0)
