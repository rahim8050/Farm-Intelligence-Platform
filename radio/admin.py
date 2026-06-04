from django.contrib import admin

from radio.models import (
    Favorite,
    ListeningHistory,
    Provider,
    Station,
    StationHealthCheck,
)


@admin.register(Provider)
class ProviderAdmin(admin.ModelAdmin):
    list_display = ["slug", "name", "is_active"]
    list_filter = ["is_active"]
    search_fields = ["name", "slug"]


@admin.register(Station)
class StationAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "name",
        "provider",
        "country",
        "is_active",
        "is_available",
        "last_health_check_at",
    ]
    list_filter = ["is_active", "is_available", "provider", "country"]
    search_fields = ["name", "id"]


@admin.register(StationHealthCheck)
class StationHealthCheckAdmin(admin.ModelAdmin):
    list_display = [
        "station",
        "checked_at",
        "is_reachable",
        "status_code",
        "response_time_ms",
    ]
    list_filter = ["is_reachable", "station"]
    search_fields = ["station__id", "station__name"]
    readonly_fields = [
        "station",
        "checked_at",
        "is_reachable",
        "response_time_ms",
        "status_code",
        "error_message",
    ]


@admin.register(Favorite)
class FavoriteAdmin(admin.ModelAdmin):
    list_display = ["id", "user", "station", "created_at"]
    list_filter = ["station"]
    search_fields = ["user__username", "station__id", "station__name"]
    readonly_fields = ["created_at"]


@admin.register(ListeningHistory)
class ListeningHistoryAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "user",
        "station",
        "started_at",
        "ended_at",
        "ip_address",
    ]
    list_filter = ["station"]
    search_fields = ["user__username", "station__id", "station__name"]
    readonly_fields = [
        "user",
        "station",
        "started_at",
        "ended_at",
        "ip_address",
        "user_agent",
    ]
