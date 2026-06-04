"""URL configuration for the ``alerts`` app.

All endpoints are mounted under ``/api/v1/alerts/`` (see
``config/urls.py``). The paths are stable per AGENTS.md.
"""

from __future__ import annotations

from django.urls import path

from alerts.views import (
    AdminBroadcastView,
    AlertDetailView,
    AlertListView,
    SubscriptionDetailView,
    SubscriptionListCreateView,
)

app_name = "alerts"

urlpatterns = [
    path(
        "alerts/subscriptions/",
        SubscriptionListCreateView.as_view(),
        name="subscriptions",
    ),
    path(
        "alerts/subscriptions/<uuid:sub_id>/",
        SubscriptionDetailView.as_view(),
        name="subscription-detail",
    ),
    path("alerts/", AlertListView.as_view(), name="alerts"),
    path(
        "alerts/<uuid:alert_id>/",
        AlertDetailView.as_view(),
        name="alert-detail",
    ),
    path(
        "alerts/admin/send/",
        AdminBroadcastView.as_view(),
        name="admin-send",
    ),
]
