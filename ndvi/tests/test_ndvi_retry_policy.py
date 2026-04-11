from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ndvi.engines.sentinelhub import (
    SentinelHubAuthError,
    SentinelHubUpstreamError,
)
from ndvi.raster.sentinelhub_engine import (
    SentinelHubRasterError,
)
from ndvi.retry_policy import (
    RetryCategory,
    UpstreamFailureError,
    classify_status_code,
    parse_retry_after,
    should_retry,
)
from ndvi.stac_client import (
    StacProcessingError,
    StacUpstreamError,
    StacWafBlockedError,
)


def test_should_retry_waf_is_not_retryable() -> None:
    decision = should_retry(StacWafBlockedError("blocked"))
    assert decision.retry is False
    assert decision.reason.startswith(f"{RetryCategory.WAF}:")


def test_should_retry_generic_exception_is_not_retryable() -> None:
    decision = should_retry(RuntimeError("boom"))
    assert decision.retry is False
    assert decision.reason.startswith(f"{RetryCategory.UNKNOWN}:")


def test_should_retry_rate_limit_is_retryable() -> None:
    decision = should_retry(StacUpstreamError("rate limited", status_code=429))
    assert decision.retry is True
    assert decision.reason.startswith(f"{RetryCategory.RATE_LIMIT}:")


def test_should_retry_validation_is_not_retryable() -> None:
    decision = should_retry(StacUpstreamError("bad request", status_code=400))
    assert decision.retry is False
    assert decision.reason.startswith(f"{RetryCategory.VALIDATION}:")


def test_should_retry_auth_is_not_retryable() -> None:
    decision = should_retry(SentinelHubAuthError(401))
    assert decision.retry is False
    assert decision.reason.startswith(f"{RetryCategory.AUTH}:")


def test_should_retry_transient_upstream_is_retryable() -> None:
    decision = should_retry(
        SentinelHubUpstreamError(503, "service unavailable")
    )
    assert decision.retry is True
    assert decision.reason.startswith(f"{RetryCategory.TRANSIENT_UPSTREAM}:")


def test_should_retry_raster_400_is_not_retryable() -> None:
    decision = should_retry(SentinelHubRasterError(400, "bad request"))
    assert decision.retry is False
    assert decision.reason.startswith(f"{RetryCategory.VALIDATION}:")


def test_should_retry_custom_failure_uses_attributes() -> None:
    class CustomFailureError(UpstreamFailureError):
        def __init__(self) -> None:
            super().__init__(
                "custom",
                retryable=True,
                category=RetryCategory.TRANSIENT_UPSTREAM,
                status_code=503,
                delay=2.5,
            )

    decision = should_retry(CustomFailureError())
    assert decision.retry is True
    assert decision.delay == 2.5
    assert decision.reason.startswith(f"{RetryCategory.TRANSIENT_UPSTREAM}:")


# ---------------------------------------------------------------------------
# classify_status_code truth table tests (single source of truth)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status_code", "expected_retryable", "expected_category"),
    [
        (400, False, RetryCategory.VALIDATION),
        (401, False, RetryCategory.AUTH),
        (403, False, RetryCategory.AUTH),
        (422, False, RetryCategory.VALIDATION),
        (429, True, RetryCategory.RATE_LIMIT),
        (500, True, RetryCategory.TRANSIENT_UPSTREAM),
        (502, True, RetryCategory.TRANSIENT_UPSTREAM),
        (503, True, RetryCategory.TRANSIENT_UPSTREAM),
        (504, True, RetryCategory.TRANSIENT_UPSTREAM),
        (200, False, RetryCategory.UNKNOWN),
        (201, False, RetryCategory.UNKNOWN),
        (204, False, RetryCategory.UNKNOWN),
        (None, False, RetryCategory.UNKNOWN),
    ],
)
def test_classify_status_code_truth_table(
    status_code: int | None,
    expected_retryable: bool,
    expected_category: RetryCategory,
) -> None:
    retryable, category = classify_status_code(status_code)
    assert retryable is expected_retryable
    assert category is expected_category


# ---------------------------------------------------------------------------
# Additional should_retry edge cases
# ---------------------------------------------------------------------------


def test_should_retry_stac_processing_error_is_not_retryable() -> None:
    decision = should_retry(StacProcessingError("processing failed"))
    assert decision.retry is False
    assert decision.reason.startswith(f"{RetryCategory.UNKNOWN}:")


def test_should_retry_stac_upstream_with_no_status_is_not_retryable() -> None:
    """StacUpstreamError with status_code=None and no explicit retryable."""
    decision = should_retry(StacUpstreamError("no status"))
    assert decision.retry is False
    assert decision.reason.startswith(f"{RetryCategory.UNKNOWN}:")


def test_should_retry_stac_upstream_explicit_retryable() -> None:
    """StacUpstreamError with explicit retryable=True should retry."""
    decision = should_retry(StacUpstreamError("transient", retryable=True))
    assert decision.retry is True
    assert decision.reason.startswith(f"{RetryCategory.UNKNOWN}:")


def test_should_retry_sentinelhub_upstream_no_status() -> None:
    """SentinelHubUpstreamError with status_code=None."""
    decision = should_retry(SentinelHubUpstreamError(None, "network error"))
    assert decision.retry is False
    assert decision.reason.startswith(f"{RetryCategory.UNKNOWN}:")


def test_should_retry_sentinelhub_upstream_429() -> None:
    """SentinelHubUpstreamError with 429 should retry."""
    decision = should_retry(SentinelHubUpstreamError(429, "rate limited"))
    assert decision.retry is True
    assert decision.reason.startswith(f"{RetryCategory.RATE_LIMIT}:")


def test_should_retry_sentinelhub_raster_no_status() -> None:
    """SentinelHubRasterError with status_code=None."""
    decision = should_retry(SentinelHubRasterError(None, "network"))
    assert decision.retry is False
    assert decision.reason.startswith(f"{RetryCategory.UNKNOWN}:")


def test_should_retry_sentinelhub_raster_503() -> None:
    """SentinelHubRasterError with 503 should retry."""
    decision = should_retry(SentinelHubRasterError(503, "unavailable"))
    assert decision.retry is True
    assert decision.reason.startswith(f"{RetryCategory.TRANSIENT_UPSTREAM}:")


# ---------------------------------------------------------------------------
# Retry-After header parsing tests
# ---------------------------------------------------------------------------


def test_parse_retry_after_none_returns_none() -> None:
    assert parse_retry_after(None) is None


def test_parse_retry_after_empty_returns_none() -> None:
    assert parse_retry_after({}) is None


def test_parse_retry_after_numeric_delay() -> None:
    headers = {"Retry-After": "120"}
    assert parse_retry_after(headers) == 120.0


def test_parse_retry_after_case_insensitive() -> None:
    headers = {"retry-after": "60"}
    assert parse_retry_after(headers) == 60.0


def test_parse_retry_after_http_date() -> None:
    # 5 minutes from now
    future = datetime.now(UTC) + timedelta(minutes=5)
    http_date = future.strftime("%a, %d %b %Y %H:%M:%S GMT")
    headers = {"Retry-After": http_date}
    delay = parse_retry_after(headers)
    assert delay is not None
    assert 290 <= delay <= 310  # Allow 10s tolerance


def test_parse_retry_after_past_date_returns_zero() -> None:
    past = datetime.now(UTC) - timedelta(minutes=5)
    http_date = past.strftime("%a, %d %b %Y %H:%M:%S GMT")
    headers = {"Retry-After": http_date}
    delay = parse_retry_after(headers)
    assert delay is not None
    assert delay == 0.0


def test_parse_retry_after_invalid_returns_none() -> None:
    headers = {"Retry-After": "invalid"}
    assert parse_retry_after(headers) is None


def test_should_retry_429_with_retry_after_header() -> None:
    """429 with Retry-After uses server-suggested delay."""
    exc = StacUpstreamError("rate limited", status_code=429)
    headers = {"Retry-After": "90"}
    decision = should_retry(exc, response_headers=headers)
    assert decision.retry is True
    assert decision.delay == 90.0
    assert decision.reason.startswith(f"{RetryCategory.RATE_LIMIT}:")


def test_should_retry_429_without_retry_after_uses_default() -> None:
    """429 response without Retry-After should have None delay."""
    exc = StacUpstreamError("rate limited", status_code=429)
    decision = should_retry(exc, response_headers=None)
    assert decision.retry is True
    assert decision.delay is None


def test_should_retry_non_429_ignores_retry_after() -> None:
    """Non-429 responses should not use Retry-After header."""
    exc = StacUpstreamError("server error", status_code=503)
    headers = {"Retry-After": "90"}
    decision = should_retry(exc, response_headers=headers)
    assert decision.retry is True
    assert decision.delay is None  # Only 429 uses Retry-After
