from __future__ import annotations

from django.urls import path

from .farm_views import (
    FarmWeatherCurrentView,
    FarmWeatherDailyView,
    FarmWeatherHourlyView,
)
from .views import WeatherCurrentView, WeatherDailyView, WeatherWeeklyView

urlpatterns = [
    path(
        "weather/current/",
        WeatherCurrentView.as_view(),
        name="weather-current",
    ),
    path(
        "weather/daily/",
        WeatherDailyView.as_view(),
        name="weather-daily",
    ),
    path(
        "weather/weekly/",
        WeatherWeeklyView.as_view(),
        name="weather-weekly",
    ),
    path(
        "farms/<int:farm_id>/weather/current/",
        FarmWeatherCurrentView.as_view(),
        name="farm-weather-current",
    ),
    path(
        "farms/<int:farm_id>/weather/hourly/",
        FarmWeatherHourlyView.as_view(),
        name="farm-weather-hourly",
    ),
    path(
        "farms/<int:farm_id>/weather/daily/",
        FarmWeatherDailyView.as_view(),
        name="farm-weather-daily",
    ),
]
