# Routes (prefix: /api/v1/):
# - GET /integrations/nextcloud/ping/ -> NextcloudPingView

from __future__ import annotations

from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    IntegrationClientViewSet,
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
        "integrations/nextcloud/ping/",
        NextcloudPingView.as_view(),
        name="nextcloud-ping",
    ),
    path(
        "integrations/nextcloud/status/",
        NextcloudStatusView.as_view(),
        name="nextcloud-status",
    ),
    path(
        "integrations/nextcloud/preview.png",
        NextcloudPreviewView.as_view(),
        name="nextcloud-preview",
    ),
    path("ping/", IntegrationPingView.as_view(), name="integration-ping"),
    path("token/", IntegrationTokenView.as_view(), name="integration-token"),
    path(
        "whoami/", IntegrationWhoAmIView.as_view(), name="integration-whoami"
    ),
] + router.urls
