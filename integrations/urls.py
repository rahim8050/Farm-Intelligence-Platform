# Routes (prefix: /api/v1/):
# - GET /integrations/nextcloud/ping/ -> NextcloudPingView
# - GET /integrations/nextcloud/status/ -> NextcloudStatusView
# - GET /integrations/nextcloud/preview.png -> NextcloudPreviewView

from __future__ import annotations

from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    IntegrationClientViewSet,
    IntegrationHealthView,
    IntegrationPingView,
    IntegrationTokenView,
    IntegrationWhoAmIView,
    NextcloudPingView,
    NextcloudPreviewView,
    NextcloudStatusView,
)

router = DefaultRouter()
router.register(
    r"clients",
    IntegrationClientViewSet,
    basename="integration-client",
)

urlpatterns = [
    path(
        "nextcloud/ping/",
        NextcloudPingView.as_view(),
        name="nextcloud-hmac",
    ),
    path(
        "nextcloud/status/",
        NextcloudStatusView.as_view(),
        name="nextcloud-status",
    ),
    path(
        "nextcloud/preview.png",
        NextcloudPreviewView.as_view(),
        name="nextcloud-preview",
    ),
    path("ping/", IntegrationPingView.as_view(), name="integration-ping"),
    path("token/", IntegrationTokenView.as_view(), name="integration-token"),
    path(
        "health/",
        IntegrationHealthView.as_view(),
        name="integration-health",
    ),
    path(
        "whoami/", IntegrationWhoAmIView.as_view(), name="integration-whoami"
    ),
] + router.urls
