"""Farm activity endpoints.

Authentication: API key, user JWT, or integration JWT.
Integration access: allow-listed per farm via FarmIntegrationAccess.
Responses: wrapped by config.api.responses.success_response.
"""

from __future__ import annotations

from typing import Any, cast

from django.contrib.auth import get_user_model
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiTypes,
    extend_schema,
)
from rest_framework import serializers, status
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
from config.api.openapi import (
    error_envelope_serializer,
    success_envelope_serializer,
)
from config.api.responses import JSONValue, success_response
from integrations.authentication import IntegrationTokenUser

from .authentication import FarmObservationAuthentication
from .models import Farm

activity_error_schema = error_envelope_serializer("FarmActivityErrorResponse")

activity_success_schema = success_envelope_serializer(
    "FarmActivitySuccess",
    data=ActivitySerializer(),
)

activity_list_success_schema = success_envelope_serializer(
    "FarmActivityListSuccess",
    data=ActivitySerializer(many=True),
)

activity_delete_success_schema = success_envelope_serializer(
    "FarmActivityDeleteSuccess",
    data=serializers.JSONField(allow_null=True),
)

activity_list_query_params = [
    OpenApiParameter(
        name="status",
        type=OpenApiTypes.STR,
        location=OpenApiParameter.QUERY,
        required=False,
        description="Filter activities by status.",
    ),
    OpenApiParameter(
        name="type",
        type=OpenApiTypes.STR,
        location=OpenApiParameter.QUERY,
        required=False,
        description="Filter activities by type.",
    ),
    OpenApiParameter(
        name="limit",
        type=OpenApiTypes.INT,
        location=OpenApiParameter.QUERY,
        required=False,
        description="Max activities to return (default 100, max 500).",
    ),
    OpenApiParameter(
        name="offset",
        type=OpenApiTypes.INT,
        location=OpenApiParameter.QUERY,
        required=False,
        description="Number of activities to skip (default 0).",
    ),
]


class BaseFarmActivityView(APIView):
    """Shared helpers for farm activity endpoints.

    Auth: API key, user JWT, or integration JWT.
    Permissions: IsAuthenticated; owner-only for user/API key requests.
    Integration access: allow-listed per farm via FarmIntegrationAccess.
    Integration scope: read for GET, write for POST/PATCH/DELETE.
    Response envelope: success_response.
    """

    authentication_classes = (FarmObservationAuthentication,)
    permission_classes = (IsAuthenticated,)

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

    def _get_farm(self, request: Request, farm_id: int) -> Farm:
        if isinstance(request.user, IntegrationTokenUser):
            return get_object_or_404(
                Farm,
                id=farm_id,
                is_active=True,
                integration_access__client_id=request.user.client_id,
                integration_access__is_active=True,
            )

        user_id = getattr(request.user, "id", None)
        if user_id is None:
            raise Http404

        return get_object_or_404(
            Farm,
            Q(id=farm_id, owner_id=user_id, is_active=True)
            | Q(
                id=farm_id,
                integration_access__is_active=True,
                is_active=True,
            ),
        )

    def _resolve_owner(
        self, request: Request, farm: Farm | None = None
    ) -> Any:
        if isinstance(request.user, IntegrationTokenUser):
            if farm is not None:
                return farm.owner
            user_model = get_user_model()
            return user_model.objects.get(pk=request.user.token.get("user_id"))
        return request.user


class FarmActivityListCreateView(BaseFarmActivityView):
    """List and create farm activities.

    Auth: API key, user JWT, or integration JWT.
    Permissions: owner-only for user/API key requests.
    Request body: ActivityCreateSerializer (POST).
    Response data: list of activities (GET) or a single activity (POST).
    """

    @extend_schema(
        auth=cast(list[str], [{"BearerAuth": []}, {"ApiKeyAuth": []}]),
        operation_id="v1_farms_activities_list",
        parameters=activity_list_query_params,
        responses={
            200: activity_list_success_schema,
            400: activity_error_schema,
            401: activity_error_schema,
            403: activity_error_schema,
            404: activity_error_schema,
        },
    )
    def get(self, request: Request, farm_id: int) -> Response:
        """List activities for the requested farm.

        Inputs: optional query params status, type, limit, offset.
        Output: success envelope with a list of activities.
        Side effects: none.
        """

        self._enforce_integration_scope(request, write=False)
        farm = self._get_farm(request, farm_id)

        activities = Activity.objects.filter(farm=farm)
        query_status = request.query_params.get("status")
        if query_status:
            activities = activities.filter(status=query_status)
        query_type = request.query_params.get("type")
        if query_type:
            activities = activities.filter(type=query_type)

        activities = activities.order_by("-next_due_at", "-id")
        limit = int(request.query_params.get("limit") or 100)
        offset = int(request.query_params.get("offset") or 0)
        activities = activities[offset : offset + limit]
        payload = cast(
            JSONValue,
            ActivitySerializer(activities, many=True).data,
        )
        return success_response(payload, message="Farm activities")

    @extend_schema(
        auth=cast(list[str], [{"BearerAuth": []}, {"ApiKeyAuth": []}]),
        operation_id="v1_farms_activities_create",
        request=ActivityCreateSerializer,
        responses={
            201: activity_success_schema,
            400: activity_error_schema,
            401: activity_error_schema,
            403: activity_error_schema,
            404: activity_error_schema,
        },
    )
    def post(self, request: Request, farm_id: int) -> Response:
        """Create a new activity for the farm.

        Inputs: ActivityCreateSerializer body.
        Output: success envelope with the created activity.
        Side effects: creates an Activity row.
        """

        self._enforce_integration_scope(request, write=True)
        farm = self._get_farm(request, farm_id)
        serializer = ActivityCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        owner = self._resolve_owner(request, farm=farm)
        activity = serializer.save(
            farm=farm,
            owner=owner,
        )
        payload = cast(JSONValue, ActivitySerializer(activity).data)
        return success_response(
            payload,
            message="Farm activity created",
            status_code=status.HTTP_201_CREATED,
        )


class FarmActivityDetailView(BaseFarmActivityView):
    """Retrieve, update, or delete a farm activity.

    Auth: API key, user JWT, or integration JWT.
    Permissions: owner-only for user/API key requests.
    Request body: ActivityCreateSerializer (PATCH).
    Response data: a single activity (GET/PATCH) or null (DELETE).
    """

    @extend_schema(
        auth=cast(list[str], [{"BearerAuth": []}, {"ApiKeyAuth": []}]),
        operation_id="v1_farms_activities_retrieve",
        responses={
            200: activity_success_schema,
            401: activity_error_schema,
            403: activity_error_schema,
            404: activity_error_schema,
        },
    )
    def get(
        self, request: Request, farm_id: int, activity_id: int
    ) -> Response:
        """Return the requested activity entry.

        Inputs: path params farm_id, activity_id.
        Output: success envelope with the activity.
        Side effects: none.
        """

        self._enforce_integration_scope(request, write=False)
        farm = self._get_farm(request, farm_id)
        activity = get_object_or_404(Activity, id=activity_id, farm=farm)
        payload = cast(JSONValue, ActivitySerializer(activity).data)
        return success_response(payload, message="Farm activity")

    @extend_schema(
        auth=cast(list[str], [{"BearerAuth": []}, {"ApiKeyAuth": []}]),
        operation_id="v1_farms_activities_update",
        request=ActivityCreateSerializer,
        responses={
            200: activity_success_schema,
            400: activity_error_schema,
            401: activity_error_schema,
            403: activity_error_schema,
            404: activity_error_schema,
        },
    )
    def patch(
        self, request: Request, farm_id: int, activity_id: int
    ) -> Response:
        """Update fields on the activity entry.

        Inputs: path params farm_id, activity_id; body fields to update.
        Output: success envelope with the updated activity.
        Side effects: updates the Activity row.
        """

        self._enforce_integration_scope(request, write=True)
        farm = self._get_farm(request, farm_id)
        activity = get_object_or_404(Activity, id=activity_id, farm=farm)
        serializer = ActivityCreateSerializer(
            activity, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        updated = serializer.save()
        payload = cast(JSONValue, ActivitySerializer(updated).data)
        return success_response(payload, message="Farm activity updated")

    @extend_schema(
        auth=cast(list[str], [{"BearerAuth": []}, {"ApiKeyAuth": []}]),
        operation_id="v1_farms_activities_delete",
        responses={
            200: activity_delete_success_schema,
            401: activity_error_schema,
            403: activity_error_schema,
            404: activity_error_schema,
        },
    )
    def delete(
        self, request: Request, farm_id: int, activity_id: int
    ) -> Response:
        """Delete the activity entry.

        Inputs: path params farm_id, activity_id.
        Output: success envelope with data null.
        Side effects: deletes the Activity row.
        """

        self._enforce_integration_scope(request, write=True)
        farm = self._get_farm(request, farm_id)
        activity = get_object_or_404(Activity, id=activity_id, farm=farm)
        activity.delete()
        return success_response(None, message="Farm activity deleted")
