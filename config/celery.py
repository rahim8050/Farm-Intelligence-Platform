from __future__ import annotations

import os
from typing import Any

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("config")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

from .celery_metrics import register_celery_metrics  # noqa: E402

register_celery_metrics(register_collector=False, register_signals=True)


@app.task(bind=True)
def debug_task(self: Any) -> None:  # pragma: no cover - debug helper
    print(f"Request: {self.request!r}")
