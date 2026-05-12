from django.contrib import admin

from radio.models import Provider, Station


@admin.register(Provider)
class ProviderAdmin(admin.ModelAdmin):
    list_display = ["slug", "name", "is_active"]
    list_filter = ["is_active"]
    search_fields = ["name", "slug"]


@admin.register(Station)
class StationAdmin(admin.ModelAdmin):
    list_display = ["id", "name", "provider", "country", "is_active"]
    list_filter = ["is_active", "provider", "country"]
    search_fields = ["name", "id"]
