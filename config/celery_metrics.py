from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from typing import Final

from celery import current_app
from celery.signals import (  # type: ignore[import]
    task_failure,
    task_postrun,
    task_prerun,
    task_received,
    task_retry,
    task_success,
)
from django.core.cache import caches
from django.core.cache.backends.base import BaseCache
from prometheus_client import REGISTRY
from prometheus_client.core import (
    CounterMetricFamily,
    GaugeMetricFamily,
    HistogramMetricFamily,
    Metric,
)
from prometheus_client.registry import Collector

logger = logging.getLogger(__name__)

EVENTS: Final[tuple[str, ...]] = (
    "received",
    "started",
    "succeeded",
    "failed",
    "retried",
)

RUNTIME_BUCKETS: Final[tuple[float, ...]] = (
    0.1,
    0.3,
    0.5,
    1.0,
    2.0,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
    300.0,
)

START_TTL_SECONDS: Final[int] = 6 * 60 * 60

_COLLECTOR_REGISTERED = False
_SIGNALS_REGISTERED = False
_TASKS_KEY: Final[str] = "celery:metrics:tasks"


def _tasks() -> list[str]:
    cache = _cache()
    cached = cache.get(_TASKS_KEY)
    if isinstance(cached, list) and cached:
        return sorted({str(item) for item in cached if item})
    tasks = [
        name
        for name in current_app.tasks.keys()
        if not name.startswith("celery.")
    ]
    return sorted(set(tasks))


def _cache() -> BaseCache:
    return caches["default"]


def _counter_key(task: str, event: str) -> str:
    return f"celery:metrics:counter:{task}:{event}"


def _in_progress_key(task: str) -> str:
    return f"celery:metrics:in_progress:{task}"


def _runtime_count_key(task: str) -> str:
    return f"celery:metrics:runtime:count:{task}"


def _runtime_sum_key(task: str) -> str:
    return f"celery:metrics:runtime:sum_ms:{task}"


def _runtime_bucket_key(task: str, bucket: float) -> str:
    bucket_key = f"{bucket:.1f}".replace(".", "_")
    return f"celery:metrics:runtime:bucket:{task}:{bucket_key}"


def _start_key(task_id: str) -> str:
    return f"celery:metrics:start:{task_id}"


def _record_task_name(task: str) -> None:
    cache = _cache()
    cached = cache.get(_TASKS_KEY)
    if isinstance(cached, list):
        if task in cached:
            return
        updated = cached + [task]
    else:
        updated = [task]
    cache.set(_TASKS_KEY, updated)


def _incr(key: str, delta: int = 1) -> int:
    cache = _cache()
    try:
        value = cache.incr(key, delta)
    except ValueError:
        cache.add(key, delta)
        value = delta
    return int(value)


def _set(key: str, value: object, ttl: int | None = None) -> None:
    cache = _cache()
    cache.set(key, value, timeout=ttl)


def _get_int(key: str) -> int:
    cache = _cache()
    value = cache.get(key)
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _get_float(key: str) -> float | None:
    cache = _cache()
    value = cache.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _inc_event(task: str, event: str) -> None:
    if event not in EVENTS:
        return
    _record_task_name(task)
    _incr(_counter_key(task, event), 1)


def _inc_in_progress(task: str) -> None:
    _record_task_name(task)
    _incr(_in_progress_key(task), 1)


def _dec_in_progress(task: str) -> None:
    cache = _cache()
    key = _in_progress_key(task)
    try:
        value = cache.incr(key, -1)
    except ValueError:
        cache.add(key, 0)
        return
    if value < 0:
        cache.set(key, 0)


def _record_start(task_id: str) -> None:
    _set(_start_key(task_id), time.monotonic(), ttl=START_TTL_SECONDS)


def _record_runtime(task: str, duration_seconds: float) -> None:
    _record_task_name(task)
    duration_ms = int(duration_seconds * 1000)
    _incr(_runtime_count_key(task), 1)
    _incr(_runtime_sum_key(task), duration_ms)
    for bucket in RUNTIME_BUCKETS:
        if duration_seconds <= bucket:
            _incr(_runtime_bucket_key(task, bucket), 1)
    # Always track +Inf bucket by storing total count


def _consume_start(task_id: str) -> float | None:
    cache = _cache()
    key = _start_key(task_id)
    value = _get_float(key)
    cache.delete(key)
    return value


def _task_name(task: object | None, sender: object | None) -> str:
    if task is not None:
        name = getattr(task, "name", None)
        if isinstance(name, str) and name:
            return name
    if sender is not None:
        name = getattr(sender, "name", None)
        if isinstance(name, str) and name:
            return name
    return "unknown"


@task_received.connect
def _on_task_received(**kwargs: object) -> None:
    task = kwargs.get("sender")
    _inc_event(_task_name(task, None), "received")


@task_prerun.connect
def _on_task_prerun(**kwargs: object) -> None:
    task = kwargs.get("sender")
    task_id = kwargs.get("task_id")
    task_name = _task_name(task, None)
    _inc_event(task_name, "started")
    _inc_in_progress(task_name)
    if isinstance(task_id, str):
        _record_start(task_id)


@task_success.connect
def _on_task_success(**kwargs: object) -> None:
    task = kwargs.get("sender")
    _inc_event(_task_name(task, None), "succeeded")


@task_failure.connect
def _on_task_failure(**kwargs: object) -> None:
    task = kwargs.get("sender")
    _inc_event(_task_name(task, None), "failed")


@task_retry.connect
def _on_task_retry(**kwargs: object) -> None:
    task = kwargs.get("sender")
    _inc_event(_task_name(task, None), "retried")


@task_postrun.connect
def _on_task_postrun(**kwargs: object) -> None:
    task = kwargs.get("sender")
    task_id = kwargs.get("task_id")
    task_name = _task_name(task, None)
    _dec_in_progress(task_name)
    if isinstance(task_id, str):
        started = _consume_start(task_id)
        if started is not None:
            _record_runtime(task_name, time.monotonic() - started)


class CeleryMetricsCollector(Collector):
    def collect(self) -> Iterable[Metric]:
        counter = CounterMetricFamily(
            "celery_tasks_total",
            "Total Celery task events",
            labels=["task", "event"],
        )
        in_progress = GaugeMetricFamily(
            "celery_tasks_in_progress",
            "Celery tasks currently in progress",
            labels=["task"],
        )
        runtime = HistogramMetricFamily(
            "celery_task_runtime_seconds",
            "Celery task runtime in seconds",
            labels=["task"],
        )

        for task in _tasks():
            for event in EVENTS:
                counter.add_metric(
                    [task, event],
                    _get_int(_counter_key(task, event)),
                )
            in_progress.add_metric(
                [task],
                _get_int(_in_progress_key(task)),
            )
            count = _get_int(_runtime_count_key(task))
            sum_ms = _get_int(_runtime_sum_key(task))
            cumulative = 0
            buckets_data: list[tuple[str, float]] = []
            for bucket in RUNTIME_BUCKETS:
                cumulative += _get_int(_runtime_bucket_key(task, bucket))
                buckets_data.append((f"{bucket:.1f}", float(cumulative)))
            buckets_data.append(("inf", float(count)))
            runtime.add_metric(
                [task],
                buckets_data,
                sum_value=sum_ms / 1000.0,
            )

        return [counter, in_progress, runtime]


def register_celery_metrics(
    *, register_collector: bool = True, register_signals: bool = True
) -> None:
    global _COLLECTOR_REGISTERED, _SIGNALS_REGISTERED

    if register_collector and not _COLLECTOR_REGISTERED:
        REGISTRY.register(CeleryMetricsCollector())
        _COLLECTOR_REGISTERED = True

    if register_signals and not _SIGNALS_REGISTERED:
        _SIGNALS_REGISTERED = True
        logger.info("celery.metrics.signals_registered")
