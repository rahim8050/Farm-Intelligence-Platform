"""Activity scheduling API endpoints.

This module provides CRUD operations for Activity resources.

Auth: API key, user JWT, or integration JWT (FarmObservationAuthentication).
Integration access: allow-listed per farm via FarmIntegrationAccess.
Integration scope: read for GET, write for POST/PATCH/DELETE.
Response: All responses use success_response envelope.
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth import get_user_model
from django.utils import timezone
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status, viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from activities.models import Activity
from activities.serializers import (
    ActivityCreateSerializer,
    ActivitySerializer,
)
from config.api.responses import success_response
from farms.authentication import FarmObservationAuthentication
from integrations.authentication import IntegrationTokenUser

_CORRELATION_HEADER_NAMES = (
    "X-Correlation-ID",
    "X-Correlation-Id",
    "x-correlation-id",
)


def _get_correlation_id(request: Request) -> str:
    for header in _CORRELATION_HEADER_NAMES:
        value = request.headers.get(header)
        if value:
            return str(value).strip()
    return ""


class _ActivityListSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    type = serializers.CharField()
    status = serializers.CharField()
    scheduled_at = serializers.DateTimeField()
    next_due_at = serializers.DateTimeField(allow_null=True)
    recurrence_type = serializers.CharField(allow_null=True)
    interval_days = serializers.IntegerField(allow_null=True)
    cron_expression = serializers.CharField(allow_null=True)
    farm = serializers.IntegerField(allow_null=True)
    created_at = serializers.DateTimeField()


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
        "data": _ActivityListSerializer(many=True),
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

ActivityHealthData = inline_serializer(
    name="ActivityHealthData",
    fields={
        "status": serializers.CharField(),
        "timestamp": serializers.DateTimeField(),
        "total_activities": serializers.IntegerField(),
        "due_activities": serializers.IntegerField(),
        "running_activities": serializers.IntegerField(),
        "stale_candidates": serializers.IntegerField(),
    },
)

ActivityHealthEnvelope = inline_serializer(
    name="ActivityHealthEnvelope",
    fields={
        "status": serializers.IntegerField(),
        "message": serializers.CharField(),
        "data": ActivityHealthData,
        "errors": serializers.JSONField(allow_null=True),
    },
)


class ActivityViewSet(viewsets.ModelViewSet):
    """Activity CRUD operations.

    Auth: API key, user JWT, or integration JWT.
    Permissions: IsAuthenticated; owner-only for user/API key requests.
    Integration access: allow-listed per farm via FarmIntegrationAccess.
    Integration scope: read for GET, write for POST/PATCH/DELETE.
    Response: envelope with `data` = ActivitySerializer output.
    """

    serializer_class = ActivitySerializer
    authentication_classes = (FarmObservationAuthentication,)
    permission_classes = (IsAuthenticated,)
    throttle_scope = "activities"

    def _integration_scopes(self, request: Request) -> set[str]:
        auth_obj: Any = getattr(request, "auth", None)

        def _claim(key: str) -> str:
            if isinstance(auth_obj, dict):
                return str(auth_obj.get(key, "") or "")
            getter = getattr(auth_obj, "get", None)
            if callable(getter):
                try:
                    value = getter(key)
                except Exception:
                    value = None
                else:
                    if value is not None:
                        return str(value or "")
            try:
                return str(auth_obj[key] or "")
            except Exception:
                return ""

        scope = _claim("scope")
        if not scope:
            return set()
        normalized = scope.replace(",", " ")
        return {item for item in normalized.split() if item}

    def _enforce_integration_scope(
        self, request: Request, *, write: bool
    ) -> None:
        if not isinstance(request.user, IntegrationTokenUser):
            return

        scopes = self._integration_scopes(request)
        if not scopes:
            raise PermissionDenied("Integration token scope missing.")

        read_scopes = {"read", "write", "admin"}
        write_scopes = {"write", "admin"}
        allowed = write_scopes if write else read_scopes
        if not scopes.intersection(allowed):
            raise PermissionDenied("Integration token scope not permitted.")

    def get_queryset(self) -> Any:
        user = self.request.user

        if isinstance(user, IntegrationTokenUser):
            return (
                Activity.objects.filter(
                    farm__is_active=True,
                    farm__integration_access__client_id=user.client_id,
                    farm__integration_access__is_active=True,
                )
                .select_related("farm", "owner")
                .order_by("-next_due_at")
            )

        user_id = getattr(user, "id", None)
        if user_id is None:
            return Activity.objects.none()

        return (
            Activity.objects.filter(owner_id=user_id)
            .select_related("farm")
            .order_by("-next_due_at")
        )

    @extend_schema(
        responses={200: ActivityListEnvelope},
    )
    def list(
        self, request: Request, *args: object, **kwargs: object
    ) -> Response:
        """List activities.

        Returns all activities accessible to the authenticated user.
        Integration tokens require 'read' scope.
        """
        if isinstance(request.user, IntegrationTokenUser):
            self._enforce_integration_scope(request, write=False)

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
        Integration tokens require 'read' scope.
        """
        if isinstance(request.user, IntegrationTokenUser):
            self._enforce_integration_scope(request, write=False)

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

        Input: type, scheduled_at, optional recurrence_type,
               interval_days, farm, metadata.
        Output: Created activity with generated next_due_at.
        Integration tokens require 'write' scope.
        """
        if isinstance(request.user, IntegrationTokenUser):
            self._enforce_integration_scope(request, write=True)
            user_model = get_user_model()
            owner = user_model.objects.get(
                pk=request.user.token.get("user_id")
            )
        else:
            owner = request.user

        serializer = ActivityCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        metadata = dict(serializer.validated_data.get("metadata") or {})
        correlation_id = _get_correlation_id(request)
        if correlation_id:
            metadata["correlation_id"] = correlation_id
        activity = serializer.save(owner=owner, metadata=metadata)
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
        Integration tokens require 'write' scope.
        """
        if isinstance(request.user, IntegrationTokenUser):
            self._enforce_integration_scope(request, write=True)

        instance = self.get_object()
        serializer = self.get_serializer(
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
        Integration tokens require 'write' scope.
        """
        if isinstance(request.user, IntegrationTokenUser):
            self._enforce_integration_scope(request, write=True)

        instance = self.get_object()
        self.perform_destroy(instance)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ActivityHealthView(APIView):
    """Health probe for the activities engine.

    Auth: same as the rest of the activities API.
    Permissions: IsAuthenticated.
    Response: envelope with scheduler-facing health counters and timestamp.
    """

    permission_classes = (IsAuthenticated,)
    authentication_classes = (FarmObservationAuthentication,)
    throttle_scope = "activities"

    @extend_schema(responses={200: ActivityHealthEnvelope})
    def get(
        self, request: Request, *args: object, **kwargs: object
    ) -> Response:
        """Return a lightweight health snapshot for activities."""
        now = timezone.now()
        viewset = ActivityViewSet()
        viewset.request = request
        base_queryset = viewset.get_queryset()
        data = {
            "status": "ok",
            "timestamp": now,
            "total_activities": base_queryset.count(),
            "due_activities": base_queryset.filter(
                status=Activity.Status.PENDING,
                next_due_at__lte=now,
            ).count(),
            "running_activities": base_queryset.filter(
                status=Activity.Status.RUNNING,
            ).count(),
            "stale_candidates": base_queryset.filter(
                status__in=[
                    Activity.Status.DISPATCHED,
                    Activity.Status.RUNNING,
                ]
            ).count(),
        }
        return success_response(data, message="Activities health OK")
