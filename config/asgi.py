"""
ASGI config for config project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.1/howto/deployment/asgi/
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from .celery_metrics import register_celery_metrics  # noqa: E402

register_celery_metrics(register_collector=True, register_signals=False)

application = get_asgi_application()
