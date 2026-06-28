"""Structured JSON logging for the spectral index system.

Provides a ``JsonFormatter`` that outputs log records as newline-delimited
JSON, plus convenience functions for emitting structured events from
Celery tasks, compute engines, cache operations, and provider requests.

Every log line includes common fields:
  - ``request_id``
  - ``index_type``
  - ``job_id``
  - ``engine``
  - ``provider``
  - ``event``
  - ``duration_ms``

Usage::

    from ndvi.logging import StructuredLogger

    log = StructuredLogger(__name__)
    log.info("engine.compute", index_type="NDMI", engine="stac",
             duration_ms=1234, event="compute_done")
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from typing import Any

# ---------------------------------------------------------------------------
# JSON formatter
# ---------------------------------------------------------------------------


class JsonFormatter(logging.Formatter):
    """Format log records as newline-delimited JSON objects.

    All standard log record attributes (levelname, name, lineno, etc.)
    are included. Extra keyword arguments passed to the logger are
    included as top-level keys.

    Usage in ``settings.py``::

        LOGGING = {
            "formatters": {
                "json": {
                    "()": "ndvi.logging.JsonFormatter",
                },
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "json",
                },
            },
        }
    """

    def format(self, record: logging.LogRecord) -> str:
        """Format the log record as JSON."""
        base: dict[str, Any] = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Add extra fields from the record
        if hasattr(record, "extra"):
            base.update(record.extra)  # type: ignore[arg-type]

        # Add exception info if present
        if record.exc_info and record.exc_info[0]:
            base["exception"] = self.formatException(record.exc_info)

        return json.dumps(base, default=str)


# ---------------------------------------------------------------------------
# Structured logger wrapper
# ---------------------------------------------------------------------------


class StructuredLogger:
    """Logger wrapper that emits structured JSON log events.

    Adds common semantic fields (request_id, index_type, job_id,
    engine, provider, event, duration_ms) to every log call.
    """

    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(name)

    def _log(
        self,
        level: int,
        event: str,
        msg: str = "",
        *,
        duration_ms: float | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        """Emit a structured log record.

        Args:
            level: Logging level constant.
            event: Event name (e.g. ``"engine.compute"``,
                ``"cache.hit"``, ``"task.start"``).
            msg: Human-readable message string.
            duration_ms: Optional duration in milliseconds.
            extra: Additional structured fields.
        """
        log_extra: dict[str, Any] = {"event": event}
        if duration_ms is not None:
            log_extra["duration_ms"] = duration_ms
        if extra:
            log_extra.update(extra)

        self._logger.log(
            level,
            "%s",
            msg,
            extra={"extra": log_extra},
        )

    def debug(
        self,
        event: str,
        msg: str = "",
        *,
        duration_ms: float | None = None,
        **extra: Any,
    ) -> None:
        """Emit a DEBUG-level structured log."""
        self._log(
            logging.DEBUG,
            event,
            msg,
            duration_ms=duration_ms,
            extra=extra,
        )

    def info(
        self,
        event: str,
        msg: str = "",
        *,
        duration_ms: float | None = None,
        **extra: Any,
    ) -> None:
        """Emit an INFO-level structured log."""
        self._log(
            logging.INFO,
            event,
            msg,
            duration_ms=duration_ms,
            extra=extra,
        )

    def warning(
        self,
        event: str,
        msg: str = "",
        *,
        duration_ms: float | None = None,
        **extra: Any,
    ) -> None:
        """Emit a WARNING-level structured log."""
        self._log(
            logging.WARNING,
            event,
            msg,
            duration_ms=duration_ms,
            extra=extra,
        )

    def error(
        self,
        event: str,
        msg: str = "",
        *,
        duration_ms: float | None = None,
        **extra: Any,
    ) -> None:
        """Emit an ERROR-level structured log."""
        self._log(
            logging.ERROR,
            event,
            msg,
            duration_ms=duration_ms,
            extra=extra,
        )

    def exception(
        self,
        event: str,
        msg: str = "",
        *,
        duration_ms: float | None = None,
        **extra: Any,
    ) -> None:
        """Emit an ERROR-level structured log with exception info."""
        self._logger.exception(
            "%s",
            msg,
            extra={
                "extra": {
                    "event": event,
                    "duration_ms": duration_ms,
                    **extra,
                }
            },
        )

    @property
    def logger(self) -> logging.Logger:
        """Return the underlying stdlib logger."""
        return self._logger


# ---------------------------------------------------------------------------
# Timing helper
# ---------------------------------------------------------------------------


class Timer:
    """Simple monotonic timer for measuring durations.

    Usage::

        timer = Timer()
        # ... do work ...
        elapsed_ms = timer.elapsed_ms()
    """

    def __init__(self) -> None:
        self._start = time.monotonic()

    def elapsed_ms(self) -> float:
        """Return elapsed time in milliseconds."""
        return (time.monotonic() - self._start) * 1000.0

    def reset(self) -> None:
        """Reset the timer."""
        self._start = time.monotonic()
