from typing import Any

from rest_framework import serializers

from radio.models import (
    EmergencyBroadcast,
    Favorite,
    ListeningHistory,
    Provider,
    Station,
)


def _upgrade_to_https(value: str) -> str:
    """Upgrade HTTP URLs to HTTPS for mixed-content safety."""
    if isinstance(value, str) and value.startswith("http://"):
        return value.replace("http://", "https://", 1)
    return value


class ProviderSerializer(serializers.ModelSerializer):
    """Serialize provider for API responses."""

    class Meta:
        model = Provider
        fields = [
            "slug",
            "name",
            "website_url",
            "logo_url",
            "is_active",
        ]

    def to_representation(self, instance: Any) -> dict:
        data = super().to_representation(instance)
        for field in ("website_url", "logo_url"):
            if data.get(field):
                data[field] = _upgrade_to_https(data[field])
        return data


class StationSerializer(serializers.ModelSerializer):
    """Serialize station metadata for API responses."""

    provider_name = serializers.CharField(
        source="provider.name", read_only=True
    )
    provider_logo_url = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Station
        fields = [
            "id",
            "name",
            "provider",
            "provider_name",
            "provider_logo_url",
            "genre",
            "country",
            "language",
            "logo_url",
            "is_active",
        ]

    def get_provider_logo_url(self, instance: Any) -> str | None:
        logo = getattr(instance.provider, "logo_url", None)
        if logo:
            return _upgrade_to_https(logo)
        return None

    def to_representation(self, instance: Any) -> dict:
        data = super().to_representation(instance)
        for field in ("logo_url",):
            if data.get(field):
                data[field] = _upgrade_to_https(data[field])
        return data


class StationDetailSerializer(StationSerializer):
    """Extended serializer with stream URL."""

    stream_url = serializers.URLField(read_only=True)
    format = serializers.CharField(read_only=True)
    bitrate = serializers.IntegerField(read_only=True)
    website_url = serializers.URLField(read_only=True)

    class Meta(StationSerializer.Meta):
        fields = StationSerializer.Meta.fields + [
            "stream_url",
            "format",
            "bitrate",
            "website_url",
        ]

    def to_representation(self, instance: Any) -> dict:
        data = super().to_representation(instance)
        for field in ("stream_url", "website_url"):
            if data.get(field):
                data[field] = _upgrade_to_https(data[field])
        return data


class FavoriteCreateSerializer(serializers.Serializer):
    """Input serializer for ``POST /api/v1/radio/favorites/``."""

    station_id = serializers.CharField(max_length=50)

    def validate_station_id(self, value: str) -> str:
        if not Station.objects.filter(id=value, is_active=True).exists():
            raise serializers.ValidationError("Station not found or inactive.")
        return value


class FavoriteSerializer(serializers.ModelSerializer):
    """Output serializer for a favorite row.

    Embeds the full :class:`StationSerializer` payload so the client
    can render a list of favorites without a second round-trip.
    """

    station = StationSerializer(read_only=True)
    station_id = serializers.CharField(source="station.id", read_only=True)

    class Meta:
        model = Favorite
        fields = ["id", "station_id", "station", "created_at"]
        read_only_fields = fields


class ListeningHistorySerializer(serializers.ModelSerializer):
    """Output serializer for a single listening-history row.

    Read-only: rows are created internally by ``StationStreamView`` or
    by future client-driven start/stop events.
    """

    station = StationSerializer(read_only=True)
    station_id = serializers.CharField(source="station.id", read_only=True)

    class Meta:
        model = ListeningHistory
        fields = [
            "id",
            "station_id",
            "station",
            "started_at",
            "ended_at",
            "ip_address",
            "user_agent",
        ]
        read_only_fields = fields


class EmergencyBroadcastSerializer(serializers.ModelSerializer):
    """Output serializer for an :class:`EmergencyBroadcast` row."""

    class Meta:
        model = EmergencyBroadcast
        fields = [
            "id",
            "title",
            "message",
            "priority",
            "starts_at",
            "ends_at",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class EmergencyBroadcastCreateSerializer(serializers.ModelSerializer):
    """Input serializer for ``POST /api/v1/radio/emergency/`` (admin only)."""

    class Meta:
        model = EmergencyBroadcast
        fields = [
            "title",
            "message",
            "priority",
            "starts_at",
            "ends_at",
            "is_active",
        ]

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        starts_at = attrs.get("starts_at")
        ends_at = attrs.get("ends_at")
        if starts_at and ends_at and ends_at <= starts_at:
            raise serializers.ValidationError(
                {"ends_at": "ends_at must be after starts_at."}
            )
        return attrs


class EmergencyBroadcastUpdateSerializer(serializers.ModelSerializer):
    """Input serializer for ``PATCH /api/v1/radio/emergency/<id>/``."""

    class Meta:
        model = EmergencyBroadcast
        fields = [
            "title",
            "message",
            "priority",
            "starts_at",
            "ends_at",
            "is_active",
        ]
        extra_kwargs = {
            "title": {"required": False},
            "message": {"required": False},
            "priority": {"required": False},
            "starts_at": {"required": False},
            "ends_at": {"required": False},
            "is_active": {"required": False},
        }

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        starts_at = attrs.get(
            "starts_at", getattr(self.instance, "starts_at", None)
        )
        ends_at = attrs.get("ends_at", getattr(self.instance, "ends_at", None))
        if starts_at and ends_at and ends_at <= starts_at:
            raise serializers.ValidationError(
                {"ends_at": "ends_at must be after starts_at."}
            )
        return attrs


class TTSSynthesizeRequestSerializer(serializers.Serializer):
    """Input serializer for ``POST /api/v1/radio/tts/``.

    The text length is capped by the server-side ``TTS_MAX_TEXT_CHARS``
    setting (default 500).
    """

    text = serializers.CharField()
    voice = serializers.CharField(required=False, allow_blank=True)

    def validate_text(self, value: str) -> str:
        from django.conf import settings as django_settings

        cap = int(getattr(django_settings, "TTS_MAX_TEXT_CHARS", 500))
        if not value.strip():
            raise serializers.ValidationError("text must not be empty.")
        if len(value) > cap:
            raise serializers.ValidationError(
                f"text exceeds max length of {cap} characters."
            )
        return value
