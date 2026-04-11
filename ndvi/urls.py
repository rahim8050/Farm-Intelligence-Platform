from __future__ import annotations

from django.urls import path

from .proxy_views import NdviIngestProxyView
from .views import (
    CircuitBreakerResetView,
    FarmStateView,
    NdviJobStatusView,
    NdviLatestView,
    NdviRasterPngView,
    NdviRasterQueueView,
    NdviRefreshView,
    NdviTimeseriesView,
)

urlpatterns = [
    path(
        "farm-state/<int:farm_id>/",
        FarmStateView.as_view(),
        name="farm-state",
    ),
    path(
        "farms/<int:farm_id>/ndvi/timeseries/",
        NdviTimeseriesView.as_view(),
        name="ndvi-timeseries",
    ),
    path(
        "farms/<int:farm_id>/ndvi/latest/",
        NdviLatestView.as_view(),
        name="ndvi-latest",
    ),
    path(
        "farms/<int:farm_id>/ndvi/refresh/",
        NdviRefreshView.as_view(),
        name="ndvi-refresh",
    ),
    path(
        "farms/<int:farm_id>/ndvi/raster.png",
        NdviRasterPngView.as_view(),
        name="ndvi-raster",
    ),
    path(
        "farms/<int:farm_id>/ndvi/raster/queue",
        NdviRasterQueueView.as_view(),
        name="ndvi-raster-queue",
    ),
    path(
        "ndvi/jobs/<int:job_id>/",
        NdviJobStatusView.as_view(),
        name="ndvi-job",
    ),
    path(
        "ndvi",
        NdviIngestProxyView.as_view(),
        name="ndvi-ingest",
    ),
    path(
        "ndvi/circuit-breaker/reset/",
        CircuitBreakerResetView.as_view(),
        name="ndvi-circuit-breaker-reset",
    ),
]
