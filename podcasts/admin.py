from django.contrib import admin

from podcasts.models import Podcast, PodcastEpisode


@admin.register(Podcast)
class PodcastAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "title",
        "author",
        "is_active",
        "last_refreshed_at",
        "last_refresh_status",
    ]
    list_filter = ["is_active", "last_refresh_status"]
    search_fields = ["id", "title", "author", "feed_url"]
    readonly_fields = [
        "last_refreshed_at",
        "last_refresh_status",
        "last_refresh_error",
        "created_at",
        "updated_at",
    ]


@admin.register(PodcastEpisode)
class PodcastEpisodeAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "podcast",
        "title",
        "published_at",
        "duration_seconds",
        "audio_mime_type",
    ]
    list_filter = ["podcast"]
    search_fields = ["title", "guid", "podcast__id", "podcast__title"]
    readonly_fields = ["created_at", "updated_at"]
