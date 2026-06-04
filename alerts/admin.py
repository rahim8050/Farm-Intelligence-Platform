"""Django admin for the ``alerts`` app."""

from __future__ import annotations

from django.contrib import admin

from alerts.models import AudioAlert, AudioAlertSubscription


@admin.register(AudioAlertSubscription)
class AudioAlertSubscriptionAdmin(admin.ModelAdmin):
    list_display = ("user", "farm", "updated_at")
    list_filter = ("farm",)
    search_fields = ("user__username", "farm__name")


@admin.register(AudioAlert)
class AudioAlertAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "farm",
        "alert_type",
        "is_delivered",
        "is_acknowledged",
        "created_at",
    )
    list_filter = ("alert_type", "is_delivered", "is_acknowledged")
    search_fields = ("user__username", "farm__name", "title", "message")
    readonly_fields = (
        "id",
        "user",
        "farm",
        "alert_type",
        "trigger_source",
        "title",
        "message",
        "audio_file",
        "duration_ms",
        "mime_type",
        "source_object_id",
        "is_delivered",
        "is_acknowledged",
        "delivered_at",
        "acknowledged_at",
        "created_at",
    )
