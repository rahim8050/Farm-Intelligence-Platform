"""Activity scheduling API endpoints.

This module provides CRUD operations for Activity resources.

Auth: IsAuthenticated
Response: All responses use success_response envelope.
"""

from typing import Any

from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status, viewsets
from rest_framework.request import Request
from rest_framework.response import Response

from activities.models import Activity
from activities.serializers import (
    ActivityCreateSerializer,
    ActivitySerializer,
)
from config.api.responses import success_response

ActivityListSerializer = inline_serializer(
    name="ActivityListSerializer",
    fields={
        "id": serializers.IntegerField(),
        "type": serializers.CharField(),
        "status": serializers.CharField(),
        "scheduled_at": serializers.DateTimeField(),
        "next_due_at": serializers.DateTimeField(allow_null=True),
        "recurrence_type": serializers.CharField(allow_null=True),
        "interval_days": serializers.IntegerField(allow_null=True),
        "farm": serializers.IntegerField(allow_null=True),
        "created_at": serializers.DateTimeField(),
    },
)

ActivityEnvelope = inline_serializer(
    name="ActivityEnvelope",
    fields={
        "status": serializers.IntegerField(),
        "message": serializers.CharField(),
        "data": ActivitySerializer(),
        "errors": serializers.JSONField(allow_null=True),
    },
)

ActivityListEnvelope = inline_serializer(
    name="ActivityListEnvelope",
    fields={
        "status": serializers.IntegerField(),
        "message": serializers.CharField(),
        "data": ActivityListSerializer(many=True),
        "errors": serializers.JSONField(allow_null=True),
    },
)

NullEnvelope = inline_serializer(
    name="NullEnvelope",
    fields={
        "status": serializers.IntegerField(),
        "message": serializers.CharField(),
        "data": serializers.JSONField(allow_null=True),
        "errors": serializers.JSONField(allow_null=True),
    },
)


class ActivityViewSet(viewsets.ModelViewSet):
    """Activity CRUD operations.

    Auth: IsAuthenticated
    Response: envelope with `data` = ActivitySerializer output.
    """

    serializer_class = ActivitySerializer

    def get_queryset(self) -> Any:
        user = self.request.user
        return Activity.objects.filter(owner=user).select_related("farm")

    @extend_schema(
        responses={200: ActivityListEnvelope},
    )
    def list(
        self, request: Request, *args: object, **kwargs: object
    ) -> Response:
        """List user's activities.

        Returns all activities owned by the authenticated user.
        """
        queryset = self.filter_queryset(self.get_queryset())
        serializer = self.get_serializer(queryset, many=True)
        return success_response(list(serializer.data))

    @extend_schema(
        responses={200: ActivityEnvelope},
    )
    def retrieve(
        self, request: Request, *args: object, **kwargs: object
    ) -> Response:
        """Retrieve a single activity by ID.

        Returns activity details for the authenticated user.
        """
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        return success_response(serializer.data)

    @extend_schema(
        request=ActivityCreateSerializer,
        responses={201: ActivityEnvelope},
    )
    def create(
        self, request: Request, *args: object, **kwargs: object
    ) -> Response:
        """Create a new activity.

        Input: type, scheduled_at, optional recurrence_type, interval_days, farm, metadata.
        Output: Created activity with generated next_due_at.
        """
        serializer = ActivityCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        activity = serializer.save(owner=request.user)
        return success_response(
            ActivitySerializer(activity).data,
            status_code=status.HTTP_201_CREATED,
        )

    @extend_schema(
        request=ActivitySerializer,
        responses={200: ActivityEnvelope},
    )
    def partial_update(
        self, request: Request, *args: object, **kwargs: object
    ) -> Response:
        """Update an activity (partial).

        Input: Any subset of activity fields.
        Output: Updated activity.
        """
        instance = self.get_object()
        serializer = ActivitySerializer(
            instance, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        activity = serializer.save()
        return success_response(ActivitySerializer(activity).data)

    @extend_schema(
        responses={204: NullEnvelope},
    )
    def destroy(
        self, request: Request, *args: object, **kwargs: object
    ) -> Response:
        """Delete an activity.

        Output: 204 No Content on success.
        """
        instance = self.get_object()
        self.perform_destroy(instance)
        return Response(status=status.HTTP_204_NO_CONTENT)
