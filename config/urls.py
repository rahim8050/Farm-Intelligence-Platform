"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

# Routes:
# - GET / -> home
# - /admin/ -> Django admin
# - /api/schema/ -> OpenAPI schema
# - /api/docs/ -> Swagger UI
# - /api/redoc/ -> ReDoc
# - /api/v1/auth/ -> accounts.urls
# - /api/v1/keys/ -> api_keys.urls
# - /api/v1/integrations/ -> integrations.urls

from django.contrib import admin
from django.http import HttpResponse
from django.urls import include, path
from django.views.generic import RedirectView
from drf_spectacular.views import (
    SpectacularRedocView,
    SpectacularSwaggerView,
)

from activities.views import ActivityHealthView

from .views import CachedSpectacularAPIView, home, prometheus_metrics

urlpatterns = [
    path("admin/", admin.site.urls),
    path(
        "favicon.ico",
        lambda _: HttpResponse(status=204),
        name="favicon-ico",
    ),
    path(
        "apple-touch-icon.png",
        lambda _: HttpResponse(status=204),
        name="apple-touch-icon",
    ),
    path(
        "apple-touch-icon-precomposed.png",
        lambda _: HttpResponse(status=204),
        name="apple-touch-icon-precomposed",
    ),
    path("", home, name="home"),
    path("metrics", prometheus_metrics, name="prometheus-metrics"),
    path("metrics/", prometheus_metrics, name="prometheus-metrics-slash"),
    path("", include("django_prometheus.urls")),
    path(
        "api/schema/",
        CachedSpectacularAPIView.as_view(),
        name="schema",
    ),
    path(
        "api/docs",
        RedirectView.as_view(url="/api/docs/", permanent=True),
        name="swagger-ui-redirect",
    ),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    path(
        "api/redoc/",
        SpectacularRedocView.as_view(url_name="schema"),
        name="redoc",
    ),
    path("api/v1/auth/", include("accounts.urls")),
    path("api/v1/keys/", include("api_keys.urls")),
    path("api/v1/integration/", include("integrations.urls")),
    path("api/v1/integrations/", include("integrations.urls")),
    path("api/v1/", include("farms.urls")),
    path("api/v1/", include("ndvi.urls")),
    path("api/v1/", include("weather.urls")),
    path(
        "api/v1/activities/health/",
        ActivityHealthView.as_view(),
        name="activity-health",
    ),
    path("api/v1/", include("activities.urls")),
    path("api/v1/", include("radio.urls")),
]
