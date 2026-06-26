import datetime

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
            "cron_expression",
            "farm",
            "metadata",
            "execution_id",
            "execution_started_at",
            "execution_completed_at",
            "retry_count",
            "max_retries",
            "last_error",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "status",
            "next_due_at",
            "last_executed_at",
            "execution_id",
            "execution_started_at",
            "execution_completed_at",
            "retry_count",
            "max_retries",
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
            "cron_expression",
            "farm",
            "metadata",
        ]

    def validate(self, attrs: dict) -> dict:
        recurrence_type = attrs.get("recurrence_type")
        interval_days = attrs.get("interval_days")
        cron_expression = attrs.get("cron_expression")

        if recurrence_type == Activity.RecurrenceType.INTERVAL:
            if not interval_days:
                raise serializers.ValidationError(
                    {"interval_days": "interval_days required for interval"}
                )
            if interval_days < 1:
                raise serializers.ValidationError(
                    {"interval_days": "interval_days must be at least 1"}
                )

        if recurrence_type == Activity.RecurrenceType.CRON:
            if not cron_expression:
                raise serializers.ValidationError(
                    {"cron_expression": "cron_expression required for cron"}
                )
            try:
                Activity._compute_cron_next(
                    cron_expression, datetime.datetime.now()
                )
            except (ValueError, KeyError, IndexError) as e:
                raise serializers.ValidationError(
                    {"cron_expression": f"Invalid cron expression: {e}"}
                ) from None

        valid_types = [t[0] for t in Activity.Type.choices]
        if attrs.get("type") not in valid_types:
            raise serializers.ValidationError(
                {"type": f"type must be one of: {', '.join(valid_types)}"}
            )

        return attrs
