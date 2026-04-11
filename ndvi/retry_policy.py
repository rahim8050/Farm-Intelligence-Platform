from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum


class RetryCategory(StrEnum):
    """Canonical failure categories used for retry decisions.

    This enum is the **single source of truth** for classifying HTTP errors
    and provider-specific exceptions across all NDVI engines and raster
    pipelines.
    """

    AUTH = "AUTH"
    VALIDATION = "VALIDATION"
    WAF = "WAF"
    RATE_LIMIT = "RATE_LIMIT"
    TRANSIENT_UPSTREAM = "TRANSIENT_UPSTREAM"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class RetryDecision:
    """Immutable retry decision returned by ``should_retry()``."""

    retry: bool
    delay: float | None
    reason: str


def classify_status_code(
    status_code: int | None,
) -> tuple[bool, RetryCategory]:
    """Classify an HTTP status code into a retry decision.

    This is the **single source of truth** for mapping HTTP status codes
    to retry categories across all NDVI error types:

    ============================= ========= ==========================
    Status Code                   Retryable Category
    ============================= ========= ==========================
    401, 403                      False     AUTH
    400, 422                      False     VALIDATION
    429                           True      RATE_LIMIT
    >= 500                        True      TRANSIENT_UPSTREAM
    Anything else / None          False     UNKNOWN
    ============================= ========= ==========================

    Non-status-code categories (detected by body/content inspection):

    ============================= ========= ==========================
    Detection Method              Retryable Category
    ============================= ========= ==========================
    WAF HTML body                 False     WAF
    Processing error (raster)     False     UNKNOWN (or custom)
    ============================= ========= ==========================

    Args:
        status_code: The HTTP status code from the upstream response,
            or ``None`` if the request failed before receiving a response
            (e.g., network timeout, DNS failure).

    Returns:
        A tuple of ``(retryable, category)``.
    """
    if status_code in (401, 403):
        return False, RetryCategory.AUTH
    if status_code in (400, 422):
        return False, RetryCategory.VALIDATION
    if status_code == 429:
        return True, RetryCategory.RATE_LIMIT
    if status_code is not None and status_code >= 500:
        return True, RetryCategory.TRANSIENT_UPSTREAM
    return False, RetryCategory.UNKNOWN


# Backwards-compatible alias for internal use.
_status_retry_classification = classify_status_code


def parse_retry_after(
    response_headers: dict[str, str] | None,
) -> float | None:
    """Parse ``Retry-After`` header value.

    Supports both delay-seconds (numeric) and HTTP-date formats.
    Returns the delay in seconds, or ``None`` if the header is absent
    or unparseable.

    Args:
        response_headers: HTTP response headers (case-insensitive keys).

    Returns:
        Delay in seconds (>= 0), or ``None``.
    """
    if not response_headers:
        return None

    # Case-insensitive header lookup
    raw: str | None = None
    for key, value in response_headers.items():
        if key.lower() == "retry-after":
            raw = value
            break

    if raw is None:
        return None

    # Try numeric delay first
    try:
        delay = float(raw)
        if delay >= 0:
            return delay
    except (ValueError, TypeError):
        pass

    # Try HTTP-date format (e.g., "Wed, 21 Oct 2026 07:28:00 GMT")
    try:
        retry_time = parsedate_to_datetime(raw)
        if retry_time.tzinfo is None:
            retry_time = retry_time.replace(tzinfo=UTC)
        delay = (retry_time - datetime.now(UTC)).total_seconds()
        return max(delay, 0.0)
    except (ValueError, TypeError):
        return None


class NdviFailureError(RuntimeError):
    """Base class for NDVI failures that carry retry metadata."""

    retryable: bool = False
    category: RetryCategory = RetryCategory.UNKNOWN

    def __init__(
        self,
        message: str,
        *,
        retryable: bool | None = None,
        category: RetryCategory | None = None,
    ) -> None:
        if retryable is not None:
            self.retryable = retryable
        if category is not None:
            self.category = category
        super().__init__(message)


class UpstreamFailureError(NdviFailureError):
    """Base class for upstream/provider failures."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        category: RetryCategory,
        status_code: int | None = None,
        delay: float | None = None,
    ) -> None:
        if status_code is not None and category == RetryCategory.UNKNOWN:
            derived_retryable, derived_category = _status_retry_classification(
                status_code
            )
            retryable = retryable or derived_retryable
            category = derived_category
        self.status_code = status_code
        self.delay = delay
        super().__init__(message, retryable=retryable, category=category)


def should_retry(
    exception: BaseException,
    response_headers: dict[str, str] | None = None,
) -> RetryDecision:
    """Return the retry decision for an exception.

    Args:
        exception: The exception to evaluate.
        response_headers: Optional HTTP response headers (used to
            extract ``Retry-After`` for 429 rate-limit responses).

    Returns:
        A ``RetryDecision`` with retry flag, delay, and reason.
    """

    retryable = getattr(exception, "retryable", None)
    category = getattr(exception, "category", RetryCategory.UNKNOWN)
    delay = getattr(exception, "delay", None)

    status_code = getattr(exception, "status_code", None)
    if status_code is not None and category == RetryCategory.UNKNOWN:
        retryable, category = _status_retry_classification(status_code)

    # Extract Retry-After delay from response headers for 429 responses
    if delay is None and status_code == 429:
        delay = parse_retry_after(response_headers)

    if retryable is None:
        retryable = False

    if not isinstance(exception, NdviFailureError):
        category = RetryCategory.UNKNOWN
        if isinstance(exception, Exception):
            message = exception.__class__.__name__
        else:
            message = type(exception).__name__
        return RetryDecision(
            retry=False,
            delay=None,
            reason=f"{category}:{message}",
        )

    return RetryDecision(
        retry=bool(retryable),
        delay=delay,
        reason=f"{category}:{exception.__class__.__name__}",
    )
