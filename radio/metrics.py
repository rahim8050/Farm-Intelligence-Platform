"""Radio Prometheus metrics.

Exports the metrics described in
``docs/architecture/radio/09_operational.md``:

- ``radio_stations_total`` gauge (active station count)
- ``radio_station_health_failures_total`` counter (failed probes)
- ``radio_station_health_successes_total`` counter (successful probes)
- ``radio_station_health_latency_seconds`` histogram (probe latency)
- ``radio_health_checks_last_run_timestamp`` gauge (heartbeat)

Used by ``radio.tasks.check_all_stations_health`` and exported via the
``/metrics`` endpoint through the standard ``prometheus_client``
registry.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

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
