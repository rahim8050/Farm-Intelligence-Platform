from rest_framework import serializers

from activities.models import Activity


class ActivitySerializer(serializers.ModelSerializer):
    class Meta:
        model = Activity
        fields = [
            "id",
            "type",
            "status",
            "scheduled_at",
            "next_due_at",
            "last_executed_at",
            "recurrence_type",
            "interval_days",
            "farm",
            "metadata",
            "retry_count",
            "last_error",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "status",
            "next_due_at",
            "last_executed_at",
            "retry_count",
            "last_error",
            "created_at",
            "updated_at",
        ]


class ActivityCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Activity
        fields = [
            "type",
            "scheduled_at",
            "recurrence_type",
            "interval_days",
            "farm",
            "metadata",
        ]

    def validate(self, attrs: dict) -> dict:
        recurrence_type = attrs.get("recurrence_type")
        interval_days = attrs.get("interval_days")

        if recurrence_type == Activity.RecurrenceType.INTERVAL:
            if not interval_days:
                raise serializers.ValidationError(
                    {"interval_days": "interval_days required for interval"}
                )
            if interval_days < 1:
                raise serializers.ValidationError(
                    {"interval_days": "interval_days must be at least 1"}
                )

        valid_types = [t[0] for t in Activity.Type.choices]
        if attrs.get("type") not in valid_types:
            raise serializers.ValidationError(
                {"type": f"type must be one of: {', '.join(valid_types)}"}
            )

        return attrs
