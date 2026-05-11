from django.apps import AppConfig


class FarmsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "farms"

    def ready(self) -> None:
        # Ensure drf-spectacular extension discovery for custom authentication.
        from . import openapi  # noqa: F401
