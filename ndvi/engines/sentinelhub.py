"""Sentinel Hub NDVI engine using the Statistics API."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, datetime, timedelta
from typing import Any, Final, cast

import httpx
from django.conf import settings
from django.core.cache import caches

from ndvi.circuit_breaker import CircuitBreaker
from ndvi.metrics import (
    ndvi_cache_hit_total,
    ndvi_upstream_latency_seconds,
    ndvi_upstream_requests_total,
)
from ndvi.retry_policy import (
    RetryCategory,
    UpstreamFailureError,
    classify_status_code,
)

from .base import BBox, NDVIEngine, NdviPoint

logger = logging.getLogger(__name__)


DEFAULT_TIMEOUT_SECONDS: Final[float] = 20.0
DEFAULT_MAX_CLOUD: Final[int] = 30
DEFAULT_STEP_DAYS: Final[int] = 7
DEFAULT_LOOKBACK_DAYS: Final[int] = 14


def get_default_timeout_seconds() -> float:
    return float(
        getattr(
            settings,
            "NDVI_REQUEST_TIMEOUT_SECONDS",
            DEFAULT_TIMEOUT_SECONDS,
        )
    )


def get_default_max_cloud() -> int:
    return int(getattr(settings, "NDVI_DEFAULT_MAX_CLOUD", DEFAULT_MAX_CLOUD))


def get_default_step_days() -> int:
    return int(getattr(settings, "NDVI_DEFAULT_STEP_DAYS", DEFAULT_STEP_DAYS))


def get_default_lookback_days() -> int:
    return int(
        getattr(settings, "NDVI_DEFAULT_LOOKBACK_DAYS", DEFAULT_LOOKBACK_DAYS)
    )


NDVI_EVALSCRIPT: Final[str] = """
//VERSION=3
function setup() {
  return {
    input: [{bands: ["B08", "B04", "SCL"]}],
    output: [
      { id: "ndvi", bands: 1, sampleType: "FLOAT32", statistics: true },
      { id: "dataMask", bands: 1 }
    ]
  };
}

const MASKED_SCL = [3, 8, 9, 10, 11]; // cloud/shadow/high-probability

function isClear(sceneClass) {
  return MASKED_SCL.indexOf(sceneClass) === -1;
}

function evaluatePixel(sample) {
  const ndvi = (sample.B08 - sample.B04) / (sample.B08 + sample.B04);
  const mask = isFinite(ndvi) && isClear(sample.SCL) ? 1 : 0;
  return { ndvi: [ndvi], dataMask: [mask] };
}
"""


class SentinelHubAuthError(UpstreamFailureError):
    """Signals Sentinel Hub authentication/authorization failures."""

    def __init__(self, status_code: int | None) -> None:
        message = "Sentinel Hub authentication failed"
        if status_code:
            message = f"{message} (status={status_code})"
        message = (
            f"{message}. Switch NDVI_ENGINE=stac or update Sentinel Hub "
            "credentials."
        )
        super().__init__(
            message,
            retryable=False,
            category=RetryCategory.AUTH,
            status_code=status_code,
        )


class SentinelHubUpstreamError(UpstreamFailureError):
    """Signals non-auth Sentinel Hub upstream failures."""

    def __init__(self, status_code: int | None, message: str) -> None:
        # Delegate classification to the single source of truth.
        retryable, category = classify_status_code(status_code)
        super().__init__(
            message,
            retryable=retryable,
            category=category,
            status_code=status_code,
        )


class SentinelHubEngine(NDVIEngine):
    """Fetch NDVI metrics from Sentinel Hub APIs."""

    engine_name: Final[str] = "sentinelhub"

    def __init__(
        self,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        cache_alias: str = "default",
        timeout_seconds: float | None = None,
        base_url: str | None = None,
    ) -> None:
        self.client_id = client_id or os.getenv("SENTINELHUB_CLIENT_ID")
        self.client_secret = client_secret or os.getenv(
            "SENTINELHUB_CLIENT_SECRET"
        )
        if not self.client_id or not self.client_secret:
            raise ValueError("Sentinel Hub client credentials are required")

        self.base_url = base_url or os.getenv(
            "SENTINELHUB_BASE_URL", "https://services.sentinel-hub.com"
        )
        self.token_url = f"{self.base_url}/oauth/token"
        self.statistics_url = f"{self.base_url}/api/v1/statistics"
        self.cache = caches[cache_alias]
        self.timeout_seconds = timeout_seconds or get_default_timeout_seconds()
        self._http = httpx.Client(timeout=self.timeout_seconds)

        # Circuit breaker configuration
        cb_threshold = int(
            getattr(settings, "NDVI_SENTINELHUB_CIRCUIT_BREAKER_THRESHOLD", 3)
        )
        cb_timeout = float(
            getattr(
                settings,
                "NDVI_SENTINELHUB_CIRCUIT_BREAKER_TIMEOUT_SECS",
                300.0,
            )
        )
        self._circuit_breaker = CircuitBreaker(
            engine="sentinelhub",
            failure_threshold=cb_threshold,
            reset_timeout_secs=cb_timeout,
        )

    def get_timeseries(
        self,
        *,
        bbox: BBox,
        start: date,
        end: date,
        step_days: int | None = None,
        max_cloud: int | None = None,
    ) -> list[NdviPoint]:
        step = step_days if step_days is not None else get_default_step_days()
        cloud = max_cloud if max_cloud is not None else get_default_max_cloud()
        payload = self._build_statistics_payload(
            bbox=bbox,
            start=start,
            end=end,
            step_days=step,
            max_cloud=cloud,
        )
        token = self._get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        response = self._request_with_retry(
            "POST",
            self.statistics_url,
            json=payload,
            headers=headers,
        )
        return self._parse_statistics_response(response.json())

    def get_latest(
        self,
        *,
        bbox: BBox,
        lookback_days: int | None = None,
        max_cloud: int | None = None,
    ) -> NdviPoint | None:
        lookback = (
            lookback_days
            if lookback_days is not None
            else get_default_lookback_days()
        )
        cloud = max_cloud if max_cloud is not None else get_default_max_cloud()
        today = date.today()
        start = today - timedelta(days=lookback)
        points = self.get_timeseries(
            bbox=bbox,
            start=start,
            end=today,
            step_days=lookback,
            max_cloud=cloud,
        )
        if not points:
            return None
        return sorted(points, key=lambda p: p.date)[-1]

    def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        max_attempts: int = 3,
    ) -> httpx.Response:
        # Check circuit breaker before each request
        if self._circuit_breaker.is_open():
            raise SentinelHubUpstreamError(
                None,
                "Sentinel Hub request blocked: circuit breaker is open. "
                "The upstream service appears to be unreachable.",
            )

        attempt = 0
        last_error: Exception | None = None
        while attempt < max_attempts:
            attempt += 1
            started = time.monotonic()
            try:
                response = self._http.request(
                    method,
                    url,
                    json=json,
                    data=data,
                    headers=headers,
                    timeout=self.timeout_seconds,
                )
                ndvi_upstream_latency_seconds.labels(
                    engine=self.engine_name
                ).observe(time.monotonic() - started)
                response.raise_for_status()
                ndvi_upstream_requests_total.labels(
                    engine=self.engine_name, outcome="success"
                ).inc()
                self._circuit_breaker.record_success()
                return response
            except httpx.HTTPStatusError as exc:
                last_error = exc
                status_code = (
                    exc.response.status_code if exc.response else None
                )
                ndvi_upstream_requests_total.labels(
                    engine=self.engine_name, outcome="error"
                ).inc()
                if status_code in (401, 403):
                    raise SentinelHubAuthError(status_code) from exc
                if (
                    status_code is not None
                    and status_code >= 500
                    and attempt < max_attempts
                ):
                    time.sleep(0.5 * attempt)
                    continue
                self._circuit_breaker.record_failure()
                raise
            except httpx.RequestError as exc:
                last_error = exc
                ndvi_upstream_requests_total.labels(
                    engine=self.engine_name, outcome="network"
                ).inc()
                if attempt < max_attempts:
                    time.sleep(0.5 * attempt)
                    continue
                # Wrap in UpstreamFailureError so should_retry() at the
                # Celery level returns retry=True for transient network errors.
                self._circuit_breaker.record_failure()
                raise SentinelHubUpstreamError(
                    None,
                    f"Sentinel Hub network error: {exc}",
                ) from exc
        if last_error:
            raise last_error
        raise RuntimeError("Unknown upstream error")

    def _get_access_token(self) -> str:
        key = f"ndvi:sentinelhub:token:{self.client_id}"
        cached = self.cache.get(key)
        if cached:
            ndvi_cache_hit_total.labels(layer="sentinel_token").inc()
            return str(cached)

        data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        response = self._request_with_retry(
            "POST",
            self.token_url,
            json=None,
            data=data,
            headers=headers,
        )
        token_data = response.json()
        token = token_data.get("access_token")
        expires_in = int(token_data.get("expires_in", 3600))
        if not token:
            raise ValueError(
                "Sentinel Hub token response missing access_token"
            )

        ttl = max(expires_in - 60, 60)
        self.cache.set(key, token, ttl)
        return str(token)

    def _build_statistics_payload(
        self,
        *,
        bbox: BBox,
        start: date,
        end: date,
        step_days: int,
        max_cloud: int,
    ) -> dict[str, Any]:
        bounds = [
            float(bbox.west),
            float(bbox.south),
            float(bbox.east),
            float(bbox.north),
        ]
        payload: dict[str, Any] = {
            "input": {
                "bounds": {"bbox": bounds},
                "data": [
                    {
                        "type": "sentinel-2-l2a",
                        "dataFilter": {
                            "maxCloudCoverage": max_cloud,
                        },
                    }
                ],
            },
            "aggregation": {
                "timeRange": {
                    "from": datetime.combine(
                        start, datetime.min.time()
                    ).isoformat()
                    + "Z",
                    "to": datetime.combine(
                        end, datetime.max.time()
                    ).isoformat()
                    + "Z",
                },
                "aggregationInterval": {"of": f"P{int(step_days)}D"},
                "evalscript": NDVI_EVALSCRIPT,
            },
            "calculations": {"default": {}},
        }
        logger.debug("sentinelhub.request payload=%s", json.dumps(payload))
        return payload

    def _parse_statistics_response(
        self, data: dict[str, Any]
    ) -> list[NdviPoint]:
        buckets: list[NdviPoint] = []
        for item in data.get("data", []):
            interval = item.get("interval", {})
            raw_from = interval.get("from") or interval.get("date")
            if not raw_from:
                continue
            try:
                bucket_date = date.fromisoformat(str(raw_from)[:10])
            except ValueError:
                continue

            outputs = item.get("outputs", {}).get("default", {})
            stats_container = (
                outputs.get("statistics") or outputs.get("bands") or {}
            )
            ndvi_stats: dict[str, Any] | None = None
            if isinstance(stats_container, dict):
                ndvi_stats = (
                    stats_container.get("ndvi")
                    or stats_container.get("NDVI")
                    or stats_container
                )
            raw_stats: dict[str, Any] = {}
            if isinstance(ndvi_stats, dict):
                raw_stats = cast(
                    dict[str, Any], ndvi_stats.get("stats") or ndvi_stats
                )

            mean_val = raw_stats.get("mean")
            if mean_val is None:
                continue
            try:
                mean = float(mean_val)
            except (TypeError, ValueError):
                continue

            min_val = raw_stats.get("min")
            max_val = raw_stats.get("max")
            sample_count = raw_stats.get("sampleCount") or raw_stats.get(
                "count"
            )
            cloud_fraction = outputs.get("cloudCoverage") or outputs.get(
                "cloudFraction"
            )

            buckets.append(
                NdviPoint(
                    date=bucket_date,
                    mean=mean,
                    min=float(min_val) if min_val is not None else None,
                    max=float(max_val) if max_val is not None else None,
                    sample_count=(
                        int(sample_count) if sample_count is not None else None
                    ),
                    cloud_fraction=(
                        float(cloud_fraction)
                        if cloud_fraction is not None
                        else None
                    ),
                )
            )
        return buckets

    def __repr__(self) -> str:  # pragma: no cover - convenience only
        return (
            "SentinelHubEngine("
            f"client_id={self.client_id}, base_url={self.base_url}, "
            f"timeout={self.timeout_seconds}"
            ")"
        )
