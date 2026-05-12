from rest_framework import serializers

from radio.models import Provider, Station


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


class StationSerializer(serializers.ModelSerializer):
    """Serialize station metadata for API responses."""

    provider_name = serializers.CharField(
        source="provider.name", read_only=True
    )

    class Meta:
        model = Station
        fields = [
            "id",
            "name",
            "provider",
            "provider_name",
            "genre",
            "country",
            "language",
            "logo_url",
            "is_active",
        ]


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
