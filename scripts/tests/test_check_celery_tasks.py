"""Tests for scripts/check_celery_tasks.py."""

from __future__ import annotations

from django.test import SimpleTestCase, override_settings


class CheckCeleryTasksTestCase(SimpleTestCase):
    """Validate the Celery task-name checker."""

    def test_main_returns_zero_when_ok(self) -> None:
        from scripts.check_celery_tasks import main

        result = main()
        self.assertEqual(result, 0)

    @override_settings(
        CELERY_BEAT_SCHEDULE={
            "bogus-entry": {
                "task": "no.such.task.exists",
                "schedule": 60,
            },
        }
    )
    def test_main_detects_bad_beat_entry(self) -> None:
        from scripts.check_celery_tasks import main

        result = main()
        self.assertEqual(result, 1)

    @override_settings(CELERY_BEAT_SCHEDULE={})
    def test_main_handles_empty_schedule(self) -> None:
        from scripts.check_celery_tasks import main

        result = main()
        self.assertEqual(result, 0)
