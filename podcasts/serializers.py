"""Podcast API serializers."""

from __future__ import annotations

from rest_framework import serializers

from podcasts.models import Podcast, PodcastEpisode


class PodcastSerializer(serializers.ModelSerializer):
    """Public-facing serializer for a :class:`Podcast` row."""

    class Meta:
        model = Podcast
        fields = [
            "id",
            "title",
            "description",
            "author",
            "feed_url",
            "image_url",
            "language",
            "is_active",
            "last_refreshed_at",
            "last_refresh_status",
        ]
        read_only_fields = fields


class PodcastEpisodeSerializer(serializers.ModelSerializer):
    """Public-facing serializer for a :class:`PodcastEpisode` row."""

    podcast_id = serializers.CharField(source="podcast.id", read_only=True)

    class Meta:
        model = PodcastEpisode
        fields = [
            "id",
            "podcast_id",
            "guid",
            "title",
            "description",
            "audio_url",
            "audio_mime_type",
            "duration_seconds",
            "published_at",
            "image_url",
        ]
        read_only_fields = fields


class PodcastEpisodeStreamSerializer(serializers.Serializer):
    """Output for the ``GET /podcasts/episodes/<id>/stream/`` endpoint."""

    audio_url = serializers.URLField()
    format = serializers.CharField()
    duration_seconds = serializers.IntegerField(allow_null=True)
    episode_title = serializers.CharField()
    podcast_title = serializers.CharField()
    podcast_id = serializers.CharField()
