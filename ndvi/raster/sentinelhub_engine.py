from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Any, Final

import httpx
from django.conf import settings
from django.core.cache import caches

from ndvi.circuit_breaker import (
    CircuitBreaker,
    get_circuit_breaker,
    register_circuit_breaker,
)
from ndvi.engines.sentinelhub import (
    SentinelHubAuthError,
    SentinelHubEngine,
    get_default_max_cloud,
    get_default_timeout_seconds,
)
from ndvi.metrics import (
    ndvi_upstream_latency_seconds,
    ndvi_upstream_requests_total,
)
from ndvi.retry_policy import UpstreamFailureError, classify_status_code

from .base import NdviRasterEngine, RasterRequest

logger = logging.getLogger(__name__)

MAX_ERROR_SNIPPET_CHARS = 1600


class SentinelHubRasterError(UpstreamFailureError):
    """Signals a non-2xx raster request from Sentinel Hub."""

    def __init__(self, status_code: int | None, snippet: str | None) -> None:
        self.status_code = status_code
        self.snippet = snippet
        message = f"Sentinel Hub raster error status={status_code}"
        if snippet:
            message = f"{message} body={snippet}"
        # Delegate classification to the single source of truth.
        retryable, category = classify_status_code(status_code)
        super().__init__(
            message,
            retryable=retryable,
            category=category,
            status_code=status_code,
        )


RASTER_EVALSCRIPT: Final[str] = """
//VERSION=3
function setup() {
  return {
    input: [{bands: ["B08", "B04", "dataMask"]}],
    output: { id: "default", bands: 4 }
  };
}

function evaluatePixel(sample) {
  const ndvi = (sample.B08 - sample.B04) / (sample.B08 + sample.B04);
  const val = isFinite(ndvi) ? ndvi : -1;
  // Simple red-yellow-green gradient
  const rgb = colorBlend(val,
    [-1.0, 0.0, 0.5, 1.0],
    [
      [0.4, 0.0, 0.0],
      [0.9, 0.5, 0.0],
      [0.0, 0.6, 0.0],
      [0.0, 0.8, 0.0],
    ]
  );
  return [rgb[0], rgb[1], rgb[2], sample.dataMask];
}
"""


class SentinelHubRasterEngine(NdviRasterEngine):
    """Render NDVI rasters via Sentinel Hub Process API."""

    engine_name: Final[str] = "sentinelhub"

    def __init__(
        self,
        *,
        cache_alias: str = "default",
        timeout_seconds: float | None = None,
        base_url: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
    ) -> None:
        self._timeout = timeout_seconds or get_default_timeout_seconds()
        self.base_url = base_url or os.getenv(
            "SENTINELHUB_BASE_URL", "https://services.sentinel-hub.com"
        )
        self.process_url = f"{self.base_url}/api/v1/process"
        self._stats = SentinelHubEngine(
            client_id=client_id,
            client_secret=client_secret,
            cache_alias=cache_alias,
            timeout_seconds=self._timeout,
            base_url=self.base_url,
        )
        self.cache = caches[cache_alias]
        self._http = httpx.Client(timeout=self._timeout)

        # Circuit breaker: reuse existing instance from AppConfig if available
        self._circuit_breaker: CircuitBreaker = (
            get_circuit_breaker("sentinelhub_raster")
            or self._init_circuit_breaker()
        )

    def _init_circuit_breaker(self) -> CircuitBreaker:
        """Create and register a new circuit breaker for raster engine."""
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
        cb = CircuitBreaker(
            engine="sentinelhub_raster",
            failure_threshold=cb_threshold,
            reset_timeout_secs=cb_timeout,
        )
        register_circuit_breaker(cb)
        return cb

    def render_png(self, request: RasterRequest) -> bytes:
        payload = self._build_payload(request)
        logger.debug(
            "ndvi.raster.payload keys=%s evalscript_len=%s",
            sorted(payload.keys()),
            len(RASTER_EVALSCRIPT),
        )
        token = self._stats._get_access_token()  # pylint: disable=protected-access
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        response = self._request_with_retry(
            "POST",
            self.process_url,
            json=payload,
            headers=headers,
        )
        return response.content

    def _build_payload(self, request: RasterRequest) -> dict[str, Any]:
        bounds = [
            float(request.bbox.west),
            float(request.bbox.south),
            float(request.bbox.east),
            float(request.bbox.north),
        ]
        day_start = datetime.combine(request.date, datetime.min.time())
        day_end = datetime.combine(request.date, datetime.max.time())
        return {
            "evalscript": RASTER_EVALSCRIPT,
            "input": {
                "bounds": {
                    "bbox": bounds,
                    "properties": {
                        "crs": "http://www.opengis.net/def/crs/OGC/1.3/CRS84"
                    },
                },
                "data": [
                    {
                        "type": "sentinel-2-l2a",
                        "dataFilter": {
                            "maxCloudCoverage": request.max_cloud
                            or get_default_max_cloud(),
                            "timeRange": {
                                "from": day_start.isoformat() + "Z",
                                "to": day_end.isoformat() + "Z",
                            },
                        },
                    }
                ],
            },
            "output": {
                "width": request.size,
                "height": request.size,
                "responses": [
                    {"identifier": "default", "format": {"type": "image/png"}}
                ],
            },
        }

    def _response_snippet(self, response: httpx.Response | None) -> str | None:
        if response is None:
            return None
        try:
            text = response.text.strip()
        except Exception:
            return None
        if not text:
            return None
        normalized = " ".join(text.splitlines())
        if len(normalized) > MAX_ERROR_SNIPPET_CHARS:
            normalized = f"{normalized[:MAX_ERROR_SNIPPET_CHARS]}..."
        return normalized

    def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        max_attempts: int = 3,
    ) -> httpx.Response:
        # Check circuit breaker before each request
        if self._circuit_breaker.is_open():
            raise SentinelHubRasterError(
                None,
                "Sentinel Hub raster request blocked:"
                " circuit breaker is open.",
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
                    headers=headers,
                    timeout=self._timeout,
                )
                ndvi_upstream_latency_seconds.labels(
                    engine="sentinelhub_raster"
                ).observe(time.monotonic() - started)
                response.raise_for_status()
                ndvi_upstream_requests_total.labels(
                    engine="sentinelhub_raster", outcome="success"
                ).inc()
                self._circuit_breaker.record_success()
                return response
            except httpx.HTTPStatusError as exc:
                last_error = exc
                status_code = (
                    exc.response.status_code if exc.response else None
                )
                ndvi_upstream_requests_total.labels(
                    engine="sentinelhub_raster", outcome="error"
                ).inc()
                if status_code in (401, 403):
                    raise SentinelHubAuthError(status_code) from exc
                snippet = self._response_snippet(exc.response)
                logger.warning(
                    "Sentinel Hub raster upstream error status=%s body=%s",
                    status_code,
                    snippet or "<empty>",
                )
                if status_code is not None and status_code >= 500:
                    if attempt < max_attempts:
                        time.sleep(0.5 * attempt)
                        continue
                self._circuit_breaker.record_failure()
                raise SentinelHubRasterError(status_code, snippet) from exc
            except httpx.RequestError as exc:
                last_error = exc
                ndvi_upstream_requests_total.labels(
                    engine="sentinelhub_raster", outcome="network"
                ).inc()
                if attempt < max_attempts:
                    time.sleep(0.5 * attempt)
                    continue
                # Wrap in UpstreamFailureError so should_retry() at the
                # Celery level returns retry=True for transient network errors.
                self._circuit_breaker.record_failure()
                raise SentinelHubRasterError(None, str(exc)) from exc
        if last_error:
            raise last_error
        raise RuntimeError("Unknown raster upstream error")
