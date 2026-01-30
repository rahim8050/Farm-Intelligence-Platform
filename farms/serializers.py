from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

from django.utils.text import slugify
from rest_framework import serializers

from .models import Farm


class FarmSerializer(serializers.ModelSerializer):
    class Meta:
        model = Farm
        fields = [
            "id",
            "name",
            "slug",
            "centroid_lat",
            "centroid_lon",
            "bbox_south",
            "bbox_west",
            "bbox_north",
            "bbox_east",
            "area_ha",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "slug", "created_at", "updated_at"]

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        # Mirror model.clean() logic early so API returns neat errors.
        farm_instance: Farm | None = (
            self.instance if isinstance(self.instance, Farm) else None
        )

        def _val(key: str) -> Decimal | None:
            default = getattr(farm_instance, key, None)
            return cast(Decimal | None, attrs.get(key, default))

        south = _val("bbox_south")
        west = _val("bbox_west")
        north = _val("bbox_north")
        east = _val("bbox_east")

        bbox_vals = [south, west, north, east]
        bbox_any = any(v is not None for v in bbox_vals)
        bbox_all = all(v is not None for v in bbox_vals)

        if bbox_any and not bbox_all:
            raise serializers.ValidationError(
                "Bounding box must include south, west, north, and east."
            )

        if bbox_all:
            if south is not None and north is not None and south >= north:
                raise serializers.ValidationError(
                    "bbox_south must be < bbox_north."
                )
            if west is not None and east is not None and west >= east:
                raise serializers.ValidationError(
                    "bbox_west must be < bbox_east."
                )

        lat = _val("centroid_lat")
        lon = _val("centroid_lon")
        centroid_any = lat is not None or lon is not None
        centroid_all = lat is not None and lon is not None
        if centroid_any and not centroid_all:
            raise serializers.ValidationError(
                "Centroid requires both centroid_lat and centroid_lon."
            )

        request = self.context.get("request")
        owner_id = getattr(getattr(request, "user", None), "id", None)
        if owner_id is not None:
            name = attrs.get("name") or getattr(self.instance, "name", None)
            if name:
                name_qs = Farm.objects.filter(owner_id=owner_id, name=name)
                if farm_instance is not None:
                    name_qs = name_qs.exclude(id=farm_instance.id)
                if name_qs.exists():
                    raise serializers.ValidationError(
                        {"name": "Farm name already exists."}
                    )

                slug = getattr(farm_instance, "slug", None)
                if not slug:
                    slug = slugify(name)[:120] or "farm"
                slug_qs = Farm.objects.filter(owner_id=owner_id, slug=slug)
                if farm_instance is not None:
                    slug_qs = slug_qs.exclude(id=farm_instance.id)
                if slug_qs.exists():
                    raise serializers.ValidationError(
                        {"name": "Farm name conflicts with an existing slug."}
                    )

        return attrs
