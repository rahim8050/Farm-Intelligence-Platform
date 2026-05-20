from django.apps import AppConfig


class IntegrationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "integrations"

    def ready(self) -> None:
        # Ensure drf-spectacular extension discovery for custom authentication.
        from . import openapi  # noqa: F401
