from __future__ import annotations

from config.celery_metrics import (
    _counter_key,
    _in_progress_key,
    _runtime_bucket_key,
    _runtime_count_key,
    _runtime_sum_key,
    _start_key,
)


def test_counter_key_format() -> None:
    key = _counter_key("ndvi.tasks.run_ndvi_job", "started")
    assert key == "celery:metrics:counter:ndvi.tasks.run_ndvi_job:started"


def test_in_progress_key_format() -> None:
    key = _in_progress_key("ndvi.tasks.run_ndvi_job")
    assert key == "celery:metrics:in_progress:ndvi.tasks.run_ndvi_job"


def test_runtime_count_key_format() -> None:
    key = _runtime_count_key("ndvi.tasks.run_ndvi_job")
    assert key == "celery:metrics:runtime:count:ndvi.tasks.run_ndvi_job"


def test_runtime_sum_key_format() -> None:
    key = _runtime_sum_key("ndvi.tasks.run_ndvi_job")
    assert key == "celery:metrics:runtime:sum_ms:ndvi.tasks.run_ndvi_job"


def test_runtime_bucket_key_format() -> None:
    key = _runtime_bucket_key("ndvi.tasks.run_ndvi_job", 1.5)
    expected = "celery:metrics:runtime:bucket:ndvi.tasks.run_ndvi_job:1_5"
    assert key == expected


def test_runtime_bucket_key_small_bucket() -> None:
    key = _runtime_bucket_key("task", 0.1)
    assert key == "celery:metrics:runtime:bucket:task:0_1"


def test_start_key_format() -> None:
    key = _start_key("abc123")
    assert key == "celery:metrics:start:abc123"
