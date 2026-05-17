from typing import Any

from rest_framework import serializers

from radio.models import Provider, Station


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
