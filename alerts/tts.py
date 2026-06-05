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
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from . import metrics
from .tts_breaker import TTSCircuitOpenError, get_breaker, get_pool

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


def _tts_max_workers() -> int:
    """Per-engine executor size. Default 4, env-overridable via
    ``TTS_MAX_WORKERS`` for ops who need to bound the worker pool."""
    raw = getattr(settings, "TTS_MAX_WORKERS", 4) or 4
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 4
    return max(1, value)


def _synth_via_executor(
    engine: str,
    backend: Callable[..., TTSResult],
    body: str,
    *,
    voice: str,
    timeout_s: float,
) -> TTSResult:
    """Run a single TTS call through the per-engine breaker + pool.

    Returns the backend result, or :func:`_sine_fallback` if the
    breaker is open or the call raised. The breaker is updated on
    every call: success closes, failure opens (after
    ``failure_threshold`` consecutive failures within the window).
    """
    breaker = get_breaker(engine)
    if breaker.is_open:
        return _sine_fallback(body)
    pool = get_pool(engine, max_workers=_tts_max_workers())
    started = time.monotonic()
    future = pool.executor.submit(
        backend, body, voice=voice, timeout_s=timeout_s
    )
    try:
        result = future.result(timeout=timeout_s + 1.0)
    except TTSCircuitOpenError:
        return _sine_fallback(body)
    except Exception as exc:  # noqa: BLE001
        metrics.render_failures(engine=engine, reason=exc.__class__.__name__)
        breaker._record_failure()  # noqa: SLF001 - internal but stable
        logger.warning(
            "TTS backend %s raised %s; falling back to sine",
            engine,
            exc.__class__.__name__,
        )
        return _sine_fallback(body)
    finally:
        elapsed = time.monotonic() - started
        metrics.render_duration(engine=engine, seconds=elapsed)
    breaker._record_success()  # noqa: SLF001
    return result


def synthesize(text: str) -> TTSResult:
    """Synthesise ``text`` into a :class:`TTSResult`.

    Reads ``settings.TTS_ENGINE`` (and ``settings.TTS_VOICE``,
    ``settings.TTS_TIMEOUT_SECONDS``). Always returns a result; on
    backend failure (or circuit-open) it falls back to the sine
    generator and logs the cause.

    The call is dispatched to a per-engine :class:`ThreadPoolExecutor`
    whose size is bounded by ``settings.TTS_MAX_WORKERS`` (default
    4). When the per-engine breaker is open (5 failures / 60s) the
    call short-circuits to sine for ``TTS_CIRCUIT_OPEN_FOR_S`` seconds
    before allowing a half-open probe. See
    :mod:`alerts.tts_breaker` and the SLO alert
    ``AlertsTTSCircuitOpen`` in ``monitoring/prometheus/alerts.yml``.
    """
    engine = (getattr(settings, "TTS_ENGINE", "espeak") or "espeak").lower()
    voice = getattr(settings, "TTS_VOICE", "en") or "en"
    timeout_s = float(getattr(settings, "TTS_TIMEOUT_SECONDS", 10.0) or 10.0)
    body = _truncate(text)
    backend = _BACKENDS.get(engine, _espeak)
    started = timezone.now()
    result = _synth_via_executor(
        engine, backend, body, voice=voice, timeout_s=timeout_s
    )
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
