#!/usr/bin/env python
"""Validate that every Celery task referenced in the Beat schedule and
routing table resolves to a registered task.

Exits non-zero on the first mismatch so CI can catch misnamed tasks
before they cause silent runtime failures.
"""

import os
import sys

# Must happen before Django is imported.
_proj_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj_root not in sys.path:
    sys.path.insert(0, _proj_root)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402
from django.conf import settings  # noqa: E402

django.setup()


def _import_all_task_modules() -> None:
    """Import every ``tasks.py`` under installed apps so that
    auto-named tasks (those without an explicit ``name=``) are
    registered in the Celery registry."""
    from django.apps import apps as django_apps

    for app_cfg in django_apps.get_app_configs():
        if app_cfg.name in ("django_celery_beat", "django_celery_results"):
            continue
        try:
            __import__(f"{app_cfg.name}.tasks")
        except ImportError:
            pass


def main() -> int:
    errors: list[str] = []

    # ── 1. Collect all registered Celery tasks ──────────────────────
    from celery import current_app

    _import_all_task_modules()
    app = current_app._get_current_object()
    registered: set[str] = set(app.tasks.keys())
    # Remove internal Celery tasks (``celery.*``)
    registered = {t for t in registered if not t.startswith("celery.")}

    # ── 2. Check Beat schedule entries ──────────────────────────────
    schedule: dict = getattr(settings, "CELERY_BEAT_SCHEDULE", {})
    for key, entry in schedule.items():
        task_name = entry.get("task", "")
        if not task_name:
            errors.append(f"Beat entry {key!r} has no 'task' field")
        elif task_name not in registered:
            errors.append(
                f"Beat entry {key!r} references {task_name!r} "
                f"but that task is NOT registered"
            )

    # ── 3. Check Celery task routing table ──────────────────────────
    from config.celery import app as celery_app

    router = celery_app.conf.task_routes or {}
    if isinstance(router, dict):
        routes = router
    elif isinstance(router, (list, tuple)):
        routes = {}
        for r in router:
            if hasattr(r, "route_for_task"):
                continue
            routes.update(r)
    else:
        routes = {}

    for task_name in routes:
        if task_name not in registered:
            errors.append(
                f"Route entry {task_name!r} is NOT a registered task"
            )

    # ── 4. Report ──────────────────────────────────────────────────
    if errors:
        print(" Celery task validation FAILED:", file=sys.stderr)
        for err in errors:
            print(f"  • {err}", file=sys.stderr)
        print(
            f"\n{len(errors)} error(s) found. "
            f"Double-check task `name=` parameters and Beat schedule.",
            file=sys.stderr,
        )
        return 1

    print(
        f" Celery task validation OK "
        f"({len(schedule)} beat entries, {len(routes)} route entries, "
        f"{len(registered)} registered tasks)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
