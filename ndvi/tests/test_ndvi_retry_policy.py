from __future__ import annotations

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
