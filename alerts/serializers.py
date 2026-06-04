"""Serializers for the ``alerts`` app."""

from __future__ import annotations

from rest_framework import serializers

from alerts.models import (
    AudioAlert,
    AudioAlertSubscription,
    AudioAlertType,
)


class AudioAlertSubscriptionSerializer(serializers.ModelSerializer):
    """Read/write serializer for :class:`AudioAlertSubscription`."""

    alert_types = serializers.ListField(
        child=serializers.CharField(),
        allow_empty=True,
    )
    farm_name = serializers.CharField(source="farm.name", read_only=True)

    class Meta:
        model = AudioAlertSubscription
        fields = [
            "id",
            "farm",
            "farm_name",
            "alert_types",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at", "farm_name"]

    def validate_alert_types(self, value: list[str]) -> list[str]:
        valid = set(AudioAlertType.values)
        bad = [v for v in value if v not in valid]
        if bad:
            raise serializers.ValidationError(
                f"Unknown alert_types: {sorted(bad)}"
            )
        return list(dict.fromkeys(value))


class AudioAlertSerializer(serializers.ModelSerializer):
    """Read serializer for :class:`AudioAlert`."""

    audio_url = serializers.SerializerMethodField()

    class Meta:
        model = AudioAlert
        fields = [
            "id",
            "farm",
            "alert_type",
            "trigger_source",
            "title",
            "message",
            "audio_url",
            "duration_ms",
            "mime_type",
            "source_object_id",
            "is_delivered",
            "is_acknowledged",
            "delivered_at",
            "acknowledged_at",
            "created_at",
        ]
        read_only_fields = fields

    def get_audio_url(self, obj: AudioAlert) -> str:
        if not obj.audio_file:
            return ""
        request = self.context.get("request")
        url = obj.audio_file.url
        if request is not None:
            return request.build_absolute_uri(url)
        return url


class AdminBroadcastSerializer(serializers.Serializer):
    """Input serializer for ``POST /api/v1/alerts/admin/send/``."""

    user_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False,
        max_length=500,
    )
    title = serializers.CharField(max_length=200)
    message = serializers.CharField()
    farm_id = serializers.IntegerField(required=False, allow_null=True)
