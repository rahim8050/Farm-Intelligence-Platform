from django.apps import AppConfig


class RadioConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "radio"
    verbose_name = "Radio"

    def ready(self) -> None:
        # Register signal handlers (station-list cache invalidation).
        # Import is intentionally inside ``ready`` to avoid touching
        # the app registry at import time.
        from radio import (
            signals,  # noqa: F401  pylint: disable=import-outside-toplevel,unused-import
        )
