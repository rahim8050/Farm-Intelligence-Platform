"""
ASGI config for config project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.1/howto/deployment/asgi/
"""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from django.core.asgi import get_asgi_application

from activities.consumers import ActivityConsumer

application = ProtocolTypeRouter({
    "http": get_asgi_application(),
    "websocket": AuthMiddlewareStack(
        URLRouter([
            # WebSocket URL patterns
            # Activity WebSocket
            ActivityConsumer.as_asgi(),
        ])
    ),
})

from .celery_metrics import register_celery_metrics  # noqa: E402

register_celery_metrics(register_collector=True, register_signals=False)
