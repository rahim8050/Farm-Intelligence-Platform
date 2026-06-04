"""Text-to-speech service for the `alerts` app.

A small pluggable TTS abstraction with four backends:

- ``piper``  - high quality neural TTS, requires the ``piper`` binary
               (or ``piper-tts`` python package) plus a voice model.
- ``espeak`` - low fidelity but ubiquitous, shells out to
               ``espeak-ng``. Default backend.
- ``sine``   - always-on fallback that emits a 1s 440Hz tone WAV.
- ``noop``   - returns empty bytes; used in tests and CI where no
               audio device or TTS engine is available.

All backends expose the same interface:

    class TTSResult(NamedTuple):
        audio_bytes: bytes
        mime_type: str
        duration_ms: int

    def synthesize(
        text: str, *, voice: str, timeout_s: float
    ) -> TTSResult: ...

The service entry point is :func:`synthesize`; it reads
``settings.TTS_ENGINE`` and dispatches to the matching backend.
"""

from __future__ import annotations

import logging
import shutil
import struct
import tempfile
import threading
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger("alerts.tts")


@dataclass(frozen=True)
class TTSResult:
    """The output of a TTS call.

    Attributes:
        audio_bytes: Raw audio bytes (WAV, MP3, or OGG).
        mime_type: MIME type matching ``audio_bytes``.
        duration_ms: Estimated playback duration in milliseconds.
    """

    audio_bytes: bytes
    mime_type: str
    duration_ms: int


def _sine_wav(duration_s: float = 1.0, freq: int = 440) -> bytes:
    """Generate a mono 16-bit PCM WAV of a sine tone. Always succeeds."""
    sample_rate = 16_000
    n_samples = int(sample_rate * duration_s)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
        path = fh.name
    try:
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            frames = bytearray()
            amplitude = 0.25 * 32_767
            for i in range(n_samples):
                sample = int(amplitude * _sin_lookup(i, freq, sample_rate))
                frames += struct.pack("<h", sample)
            w.writeframes(bytes(frames))
        return Path(path).read_bytes()
    finally:
        Path(path).unlink(missing_ok=True)


def _sin_lookup(i: int, freq: int, sample_rate: int) -> float:
    """Local sin to keep the stdlib import surface small."""
    import math

    return math.sin(2.0 * 3.141592653589793 * freq * i / sample_rate)


def _truncate(text: str) -> str:
    limit = int(getattr(settings, "TTS_MAX_TEXT_CHARS", 500))
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _piper(text: str, *, voice: str, timeout_s: float) -> TTSResult:
    import subprocess  # nosec

    binary = shutil.which("piper")
    if not binary:
        logger.warning("piper binary not on PATH; falling back to espeak")
        return _espeak(text, voice=voice, timeout_s=timeout_s)

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as out:
        out_path = out.name
    try:
        completed = subprocess.run(  # nosec
            [binary, "--output_file", out_path, "--model", voice],
            input=text.encode("utf-8"),
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"piper failed (rc={completed.returncode}): "
                f"{completed.stderr.decode('utf-8', 'replace')[:200]}"
            )
        data = Path(out_path).read_bytes()
        duration_ms = _guess_wav_duration_ms(data) or 1000
        return TTSResult(data, "audio/wav", duration_ms)
    finally:
        Path(out_path).unlink(missing_ok=True)


def _espeak(text: str, *, voice: str, timeout_s: float) -> TTSResult:
    import subprocess  # nosec

    binary = shutil.which("espeak-ng") or shutil.which("espeak")
    if not binary:
        logger.warning("espeak binary not on PATH; falling back to sine")
        return _sine_fallback(text)

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as out:
        out_path = out.name
    try:
        completed = subprocess.run(  # nosec
            [binary, "-v", voice, "-w", out_path, text],
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"espeak failed (rc={completed.returncode}): "
                f"{completed.stderr.decode('utf-8', 'replace')[:200]}"
            )
        data = Path(out_path).read_bytes()
        duration_ms = _guess_wav_duration_ms(data) or max(500, len(text) * 60)
        return TTSResult(data, "audio/wav", duration_ms)
    finally:
        Path(out_path).unlink(missing_ok=True)


def _sine_fallback(text: str) -> TTSResult:
    data = _sine_wav()
    return TTSResult(data, "audio/wav", 1000)


def _noop(text: str, *, voice: str, timeout_s: float) -> TTSResult:
    return TTSResult(b"", "audio/wav", 0)


def _guess_wav_duration_ms(data: bytes) -> int | None:
    try:
        with wave.open(__import__("io").BytesIO(data), "rb") as w:
            frames = w.getnframes()
            rate = w.getframerate()
            if rate <= 0:
                return None
            return int(1000 * frames / rate)
    except (wave.Error, EOFError, ValueError):
        return None


_BACKENDS: dict[str, Callable[..., TTSResult]] = {
    "piper": _piper,
    "espeak": _espeak,
    "sine": lambda text, **_: _sine_fallback(text),
    "noop": _noop,
}


_LOCK = threading.Lock()


def synthesize(text: str) -> TTSResult:
    """Synthesise ``text`` into a :class:`TTSResult`.

    Reads ``settings.TTS_ENGINE`` (and ``settings.TTS_VOICE``,
    ``settings.TTS_TIMEOUT_SECONDS``). Always returns a result; on
    backend failure it falls back to the sine generator and logs the
    cause.
    """
    engine = (getattr(settings, "TTS_ENGINE", "espeak") or "espeak").lower()
    voice = getattr(settings, "TTS_VOICE", "en") or "en"
    timeout_s = float(getattr(settings, "TTS_TIMEOUT_SECONDS", 10.0) or 10.0)
    body = _truncate(text)
    backend = _BACKENDS.get(engine, _espeak)
    started = timezone.now()
    with _LOCK:
        try:
            result = backend(body, voice=voice, timeout_s=timeout_s)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "TTS backend %s raised %s; falling back to sine",
                engine,
                exc.__class__.__name__,
            )
            result = _sine_fallback(body)
    logger.info(
        "tts.synthesized engine=%s chars=%d bytes=%d "
        "duration_ms=%d elapsed_ms=%d",
        engine,
        len(body),
        len(result.audio_bytes),
        result.duration_ms,
        int((timezone.now() - started).total_seconds() * 1000),
    )
    return result
