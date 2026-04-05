from __future__ import annotations

import os
from typing import Any

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("config")
app.config_from_object("django.conf:settings", namespace="CELERY")
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

from .celery_metrics import register_celery_metrics  # noqa: E402

register_celery_metrics(register_collector=False, register_signals=True)


@app.task(bind=True)
def debug_task(self: Any) -> None:  # pragma: no cover - debug helper
    print(f"Request: {self.request!r}")
