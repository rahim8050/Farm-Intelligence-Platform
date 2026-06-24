"""Tests for the standalone Celery Prometheus metrics exporter.

Covers:
- collect_metrics function
- Gauge metric registration
- Error handling for missing queues
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import prometheus_client
import pytest


@pytest.fixture(autouse=True)
def _clear_registry() -> None:
    """Reset Prometheus registry between tests."""
    prometheus_client.REGISTRY = prometheus_client.CollectorRegistry()
    yield


@patch("celery_metrics.prometheus_client.start_http_server")
def test_collect_metrics_runs_without_error(mock_start: MagicMock) -> None:
    """collect_metrics should not raise when redis is reachable."""
    from celery_metrics import collect_metrics

    fake_redis = MagicMock()
    fake_redis.llen.return_value = 5
    fake_redis.keys.return_value = [b"celery-worker-1", b"celery-worker-2"]

    with patch("redis.Redis.from_url", return_value=fake_redis):
        collect_metrics("redis://localhost:6379/0")

    fake_redis.llen.assert_called()
    fake_redis.keys.assert_called()


@patch("celery_metrics.prometheus_client.start_http_server")
def test_collect_metrics_handles_redis_error_gracefully(
    mock_start: MagicMock,
) -> None:
    """collect_metrics should not raise when redis throws."""
    from celery_metrics import collect_metrics

    fake_redis = MagicMock()
    fake_redis.llen.side_effect = ConnectionError("connection refused")
    fake_redis.keys.side_effect = ConnectionError("connection refused")

    with patch("redis.Redis.from_url", return_value=fake_redis):
        collect_metrics("redis://localhost:6379/0")


@patch("celery_metrics.prometheus_client.start_http_server")
def test_collect_metrics_sets_gauge_values(mock_start: MagicMock) -> None:
    """collect_metrics sets Prometheus gauge values."""
    from celery_metrics import collect_metrics, queue_length

    fake_redis = MagicMock()
    fake_redis.llen.return_value = 3
    fake_redis.keys.return_value = [b"celery-worker-1"]

    with patch("redis.Redis.from_url", return_value=fake_redis):
        collect_metrics("redis://localhost:6379/0")

    sample = queue_length.labels(queue="default").collect()[0].samples[0]
    assert sample.value == 3


@patch("celery_metrics.prometheus_client.start_http_server")
def test_main_starts_server(mock_start: MagicMock) -> None:
    """main should start the HTTP server."""
    from celery_metrics import main

    saved_argv = sys.argv
    sys.argv = ["celery_metrics.py", "--port", "8005"]
    try:
        with (
            patch("celery_metrics.collect_metrics") as mock_collect,
            patch("celery_metrics.time.sleep", side_effect=KeyboardInterrupt),
        ):
            with pytest.raises(KeyboardInterrupt):
                main()
    finally:
        sys.argv = saved_argv

    mock_start.assert_called_once_with(8005)
    mock_collect.assert_called_once()
