"""Farm observation endpoints.

Authentication: API key, user JWT, or integration JWT.
Integration access: allow-listed per farm via FarmIntegrationAccess.
Responses: wrapped by config.api.responses.success_response.
"""

from __future__ import annotations

from typing import Any, cast

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

from config.api.openapi import (
    error_envelope_serializer,
    success_envelope_serializer,
)
from config.api.responses import JSONValue, success_response
from integrations.authentication import IntegrationTokenUser

from .authentication import FarmObservationAuthentication
from .models import Farm, FarmObservation
from .serializers import (
    FarmObservationQuerySerializer,
    FarmObservationSerializer,
    FarmObservationWriteSerializer,
)

observation_error_schema = error_envelope_serializer(
    "FarmObservationErrorResponse"
)

observation_success_schema = success_envelope_serializer(
    "FarmObservationSuccess",
    data=FarmObservationSerializer(),
)

observation_list_success_schema = success_envelope_serializer(
    "FarmObservationListSuccess",
    data=FarmObservationSerializer(many=True),
)

observation_delete_success_schema = success_envelope_serializer(
    "FarmObservationDeleteSuccess",
    data=serializers.JSONField(allow_null=True),
)

observation_list_query_params = [
    OpenApiParameter(
        name="start",
        type=OpenApiTypes.DATETIME,
        location=OpenApiParameter.QUERY,
        required=False,
        description="Filter observations with observed_at >= start.",
    ),
    OpenApiParameter(
        name="end",
        type=OpenApiTypes.DATETIME,
        location=OpenApiParameter.QUERY,
        required=False,
        description="Filter observations with observed_at <= end.",
    ),
    OpenApiParameter(
        name="event_type",
        type=OpenApiTypes.STR,
        location=OpenApiParameter.QUERY,
        required=False,
        description="Filter observations by event_type.",
    ),
    OpenApiParameter(
        name="limit",
        type=OpenApiTypes.INT,
        location=OpenApiParameter.QUERY,
        required=False,
        description="Max observations to return (default 100, max 500).",
    ),
    OpenApiParameter(
        name="offset",
        type=OpenApiTypes.INT,
        location=OpenApiParameter.QUERY,
        required=False,
        description="Number of observations to skip (default 0).",
    ),
]


class BaseFarmObservationView(APIView):
    """Shared helpers for farm observation endpoints.

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
            | Q(id=farm_id, integration_access__is_active=True, is_active=True),
        )

    def _actor_context(
        self, request: Request
    ) -> tuple[Any | None, str | None]:
        if isinstance(request.user, IntegrationTokenUser):
            return None, request.user.client_id
        return request.user, None


class FarmObservationListCreateView(BaseFarmObservationView):
    """List and create farm observations.

    Auth: API key, user JWT, or integration JWT.
    Permissions: owner-only for user/API key requests.
    Request body: FarmObservationWriteSerializer (POST).
    Response data: list of observations (GET) or a single observation (POST).
    """

    @extend_schema(
        auth=cast(list[str], [{"BearerAuth": []}, {"ApiKeyAuth": []}]),
        operation_id="v1_farms_observations_list",
        parameters=observation_list_query_params,
        responses={
            200: observation_list_success_schema,
            400: observation_error_schema,
            401: observation_error_schema,
            403: observation_error_schema,
            404: observation_error_schema,
        },
    )
    def get(self, request: Request, farm_id: int) -> Response:
        """List observations for the requested farm.

        Inputs: optional query params start, end, event_type, limit, offset.
        Output: success envelope with a list of observations.
        Side effects: none.
        """

        self._enforce_integration_scope(request, write=False)
        farm = self._get_farm(request, farm_id)
        params = FarmObservationQuerySerializer(data=request.query_params)
        params.is_valid(raise_exception=True)
        validated = params.validated_data

        observations = FarmObservation.objects.filter(farm=farm)
        start = validated.get("start")
        if start:
            observations = observations.filter(observed_at__gte=start)
        end = validated.get("end")
        if end:
            observations = observations.filter(observed_at__lte=end)
        event_type = validated.get("event_type")
        if event_type:
            observations = observations.filter(event_type=event_type)

        observations = observations.order_by("-observed_at", "-id")
        limit = int(validated.get("limit") or 100)
        offset = int(validated.get("offset") or 0)
        observations = observations[offset : offset + limit]
        payload = cast(
            JSONValue,
            FarmObservationSerializer(observations, many=True).data,
        )
        return success_response(payload, message="Farm observations")

    @extend_schema(
        auth=cast(list[str], [{"BearerAuth": []}, {"ApiKeyAuth": []}]),
        operation_id="v1_farms_observations_create",
        request=FarmObservationWriteSerializer,
        responses={
            201: observation_success_schema,
            400: observation_error_schema,
            401: observation_error_schema,
            403: observation_error_schema,
            404: observation_error_schema,
        },
    )
    def post(self, request: Request, farm_id: int) -> Response:
        """Create a new observation for the farm.

        Inputs: FarmObservationWriteSerializer body.
        Output: success envelope with the created observation.
        Side effects: creates a FarmObservation row.
        """

        self._enforce_integration_scope(request, write=True)
        farm = self._get_farm(request, farm_id)
        serializer = FarmObservationWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        created_by, created_by_client_id = self._actor_context(request)
        observation = serializer.save(
            farm=farm,
            created_by=created_by,
            created_by_client_id=created_by_client_id,
        )
        payload = cast(JSONValue, FarmObservationSerializer(observation).data)
        return success_response(
            payload,
            message="Farm observation created",
            status_code=status.HTTP_201_CREATED,
        )


class FarmObservationDetailView(BaseFarmObservationView):
    """Retrieve, update, or delete a farm observation.

    Auth: API key, user JWT, or integration JWT.
    Permissions: owner-only for user/API key requests.
    Request body: FarmObservationWriteSerializer (PATCH).
    Response data: a single observation (GET/PATCH) or null (DELETE).
    """

    @extend_schema(
        auth=cast(list[str], [{"BearerAuth": []}, {"ApiKeyAuth": []}]),
        operation_id="v1_farms_observations_retrieve",
        responses={
            200: observation_success_schema,
            401: observation_error_schema,
            403: observation_error_schema,
            404: observation_error_schema,
        },
    )
    def get(
        self, request: Request, farm_id: int, observation_id: int
    ) -> Response:
        """Return the requested observation entry.

        Inputs: path params farm_id, observation_id.
        Output: success envelope with the observation.
        Side effects: none.
        """

        self._enforce_integration_scope(request, write=False)
        farm = self._get_farm(request, farm_id)
        observation = get_object_or_404(
            FarmObservation, id=observation_id, farm=farm
        )
        payload = cast(JSONValue, FarmObservationSerializer(observation).data)
        return success_response(payload, message="Farm observation")

    @extend_schema(
        auth=cast(list[str], [{"BearerAuth": []}, {"ApiKeyAuth": []}]),
        operation_id="v1_farms_observations_update",
        request=FarmObservationWriteSerializer,
        responses={
            200: observation_success_schema,
            400: observation_error_schema,
            401: observation_error_schema,
            403: observation_error_schema,
            404: observation_error_schema,
        },
    )
    def patch(
        self, request: Request, farm_id: int, observation_id: int
    ) -> Response:
        """Update fields on the observation entry.

        Inputs: path params farm_id, observation_id; body fields to update.
        Output: success envelope with the updated observation.
        Side effects: updates the FarmObservation row.
        """

        self._enforce_integration_scope(request, write=True)
        farm = self._get_farm(request, farm_id)
        observation = get_object_or_404(
            FarmObservation, id=observation_id, farm=farm
        )
        serializer = FarmObservationWriteSerializer(
            observation, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        updated = serializer.save()
        payload = cast(JSONValue, FarmObservationSerializer(updated).data)
        return success_response(payload, message="Farm observation updated")

    @extend_schema(
        auth=cast(list[str], [{"BearerAuth": []}, {"ApiKeyAuth": []}]),
        operation_id="v1_farms_observations_delete",
        responses={
            200: observation_delete_success_schema,
            401: observation_error_schema,
            403: observation_error_schema,
            404: observation_error_schema,
        },
    )
    def delete(
        self, request: Request, farm_id: int, observation_id: int
    ) -> Response:
        """Delete the observation entry.

        Inputs: path params farm_id, observation_id.
        Output: success envelope with data null.
        Side effects: deletes the FarmObservation row.
        """

        self._enforce_integration_scope(request, write=True)
        farm = self._get_farm(request, farm_id)
        observation = get_object_or_404(
            FarmObservation, id=observation_id, farm=farm
        )
        observation.delete()
        return success_response(None, message="Farm observation deleted")
