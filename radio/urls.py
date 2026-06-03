from django.urls import path

from radio import views

urlpatterns = [
    path(
        "radio/stations/", views.StationListView.as_view(), name="station-list"
    ),
    path(
        "radio/stations/<str:station_id>/",
        views.StationDetailView.as_view(),
        name="station-detail",
    ),
    path(
        "radio/stations/<str:station_id>/stream/",
        views.StationStreamView.as_view(),
        name="station-stream",
    ),
    path(
        "radio/providers/",
        views.ProviderListView.as_view(),
        name="provider-list",
    ),
    path(
        "radio/health/",
        views.RadioHealthView.as_view(),
        name="radio-health",
    ),
]
