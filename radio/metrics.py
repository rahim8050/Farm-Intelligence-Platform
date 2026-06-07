"""Radio Prometheus metrics.

Exports the metrics described in
``docs/architecture/radio/09_operational.md``:

- ``radio_stations_total`` gauge (active station count)
- ``radio_station_health_failures_total`` counter (failed probes)
- ``radio_station_health_successes_total`` counter (successful probes)
- ``radio_station_health_latency_seconds`` histogram (probe latency)
- ``radio_health_checks_last_run_timestamp`` gauge (heartbeat)
- ``radio_api_request_latency_seconds`` histogram (per-endpoint
  request latency in seconds, labelled by ``endpoint`` and
  ``method``)
- ``radio_api_request_errors_total`` counter (per-endpoint error
  count, labelled by ``endpoint``, ``method``, and ``status_code``)

Used by ``radio.tasks.check_all_stations_health`` and
``radio.metrics.observe_request`` (called from the radio views via
``radio.views._observe_request``) and exported via the
``/metrics`` endpoint through the standard ``prometheus_client``
registry.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from prometheus_client import Counter, Gauge, Histogram

logger = logging.getLogger("radio.metrics")

radio_stations_total = Gauge(
    "radio_stations_total",
    "Number of active radio stations",
)

radio_station_health_failures_total = Counter(
    "radio_station_health_failures_total",
    "Total number of failed station health probes",
    labelnames=["station_id", "provider_slug"],
)

radio_station_health_successes_total = Counter(
    "radio_station_health_successes_total",
    "Total number of successful station health probes",
    labelnames=["station_id", "provider_slug"],
)

radio_station_health_latency_seconds = Histogram(
    "radio_station_health_latency_seconds",
    "Latency of station health probes in seconds",
    labelnames=["station_id", "outcome"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
)

radio_health_checks_last_run_timestamp = Gauge(
    "radio_health_checks_last_run_timestamp",
    "Unix timestamp of the most recent radio health-check pass",
)

radio_api_request_latency_seconds = Histogram(
    "radio_api_request_latency_seconds",
    "Latency of radio API requests in seconds",
    labelnames=["endpoint", "method"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
)

radio_api_request_errors_total = Counter(
    "radio_api_request_errors_total",
    "Total number of failed (status >= 400) radio API requests",
    labelnames=["endpoint", "method", "status_code"],
)

ERROR_STATUS_THRESHOLD = 400


def observe_request(
    endpoint: str,
    method: str,
    status_code: int,
    duration_seconds: float,
) -> None:
    """Record one radio API request's latency and (optional) error count.

    Args:
        endpoint: Logical endpoint name (e.g. ``"stations.list"``).
        method: HTTP method in upper case (``"GET"``, ``"POST"``...).
        status_code: HTTP status code returned to the client.
        duration_seconds: Wall-clock duration of the request in seconds.
    """
    try:
        radio_api_request_latency_seconds.labels(
            endpoint=endpoint, method=method
        ).observe(max(0.0, float(duration_seconds)))
    except Exception as exc:  # noqa: BLE001 - metrics must never raise
        logger.debug(
            "radio_observe_request_latency_failed endpoint=%s method=%s "
            "err=%s",
            endpoint,
            method,
            exc.__class__.__name__,
        )
    if int(status_code) >= ERROR_STATUS_THRESHOLD:
        try:
            radio_api_request_errors_total.labels(
                endpoint=endpoint,
                method=method,
                status_code=str(int(status_code)),
            ).inc()
        except Exception as exc:  # noqa: BLE001 - metrics must never raise
            logger.debug(
                "radio_observe_request_errors_failed endpoint=%s "
                "method=%s err=%s",
                endpoint,
                method,
                exc.__class__.__name__,
            )


def timed(endpoint_name: str) -> Callable[..., Callable[..., object]]:
    """Decorator that times a radio view method and records metrics.

    The decorated method is expected to return a DRF ``Response``
    with a ``status_code`` attribute. The decorator records the
    latency histogram and increments the error counter for any
    status code ``>= 400``.
    """

    def decorator(func: Callable[..., object]) -> Callable[..., object]:
        def wrapper(
            self: object, request: object, *args: object, **kwargs: object
        ) -> object:
            method = getattr(request, "method", "GET").upper()
            start = time.monotonic()
            response = func(self, request, *args, **kwargs)
            duration = time.monotonic() - start
            status_code = getattr(response, "status_code", 200)
            observe_request(endpoint_name, method, status_code, duration)
            return response

        return wrapper

    return decorator
