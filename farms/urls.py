from __future__ import annotations

from django.urls import URLPattern, URLResolver, path
from rest_framework.routers import DefaultRouter

from .observation_views import (
    FarmObservationDetailView,
    FarmObservationListCreateView,
)
from .views import FarmViewSet

router = DefaultRouter()
router.register(r"farms", FarmViewSet, basename="farm")

urlpatterns: list[URLPattern | URLResolver] = [
    path(
        "farms/<int:farm_id>/observations/",
        FarmObservationListCreateView.as_view(),
        name="farm-observations",
    ),
    path(
        "farms/<int:farm_id>/observations/<int:observation_id>/",
        FarmObservationDetailView.as_view(),
        name="farm-observation-detail",
    ),
]
urlpatterns += router.urls
