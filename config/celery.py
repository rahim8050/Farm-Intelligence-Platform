from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import environ

BASE_DIR = Path(__file__).resolve().parent.parent
env = environ.Env()
env.read_env(BASE_DIR / ".env")


def _configure_multiprocess_metrics() -> None:
    """Enable Prometheus multiprocess mode before metrics are imported."""
    default_metrics_dir = BASE_DIR / "tmp" / "celery-metrics"
    metrics_dir = env(
        "NDVI_CELERY_METRICS_DIR",
        default=str(default_metrics_dir),
    )
    os.environ.setdefault("PROMETHEUS_MULTIPROC_DIR", metrics_dir)
    Path(metrics_dir).mkdir(parents=True, exist_ok=True)


_configure_multiprocess_metrics()

# Prometheus multiprocess mode must be configured before these imports.
# isort: off
from celery import Celery  # noqa: E402
from celery.signals import worker_ready  # type: ignore[import-untyped]  # noqa: E402
from prometheus_client import CollectorRegistry, start_http_server  # noqa: E402
from prometheus_client.multiprocess import MultiProcessCollector  # noqa: E402
# isort: on

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("config")
app.config_from_object("django.conf:settings", namespace="CELERY")
logger = logging.getLogger(__name__)

_metrics_server_started = False
app.autodiscover_tasks()

# Memory and resource limits to prevent worker degradation
app.conf.update(
    # Worker memory limits (prevent memory leaks)
    worker_max_tasks_per_child=100,  # Restart worker after 100 tasks
    worker_max_memory_per_child=512000,  # 512MB in KB
    # Task time limits (prevent hung tasks)
    task_time_limit=300,  # 5 min hard limit (kills worker thread)
    task_soft_time_limit=240,  # 4 min soft limit (raises exception)
    # Prefetch settings (control memory usage)
    worker_prefetch_multiplier=1,  # Fetch 1 task at a time
    # Task acknowledgment
    task_acks_late=True,  # Ack after task completion
)

from .celery_metrics import (  # noqa: E402
    CeleryMetricsCollector,
    register_celery_metrics,
)

register_celery_metrics(register_collector=False, register_signals=True)


def _start_metrics_server() -> None:
    """Expose worker metrics on a dedicated Prometheus HTTP port."""
    global _metrics_server_started
    if _metrics_server_started:
        return

    from django.conf import settings

    metrics_port = int(getattr(settings, "NDVI_CELERY_METRICS_PORT", 0))
    if metrics_port <= 0:
        return

    registry = CollectorRegistry()
    MultiProcessCollector(registry)
    registry.register(CeleryMetricsCollector())
    start_http_server(metrics_port, registry=registry)
    _metrics_server_started = True
    logger.info("NDVI celery metrics available on :%s", metrics_port)


@worker_ready.connect
def _on_worker_ready(**kwargs: object) -> None:
    _start_metrics_server()


@app.task(bind=True)
def debug_task(self: Any) -> None:  # pragma: no cover - debug helper
    print(f"Request: {self.request!r}")
