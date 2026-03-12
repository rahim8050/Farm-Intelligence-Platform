"""Farm sync endpoint for external integrations.

Authentication: integration JWT (Bearer token).
Responses: wrapped by config.api.responses.success_response.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from config.api.openapi import (
    error_envelope_serializer,
    success_envelope_serializer,
)
from config.api.responses import JSONValue, error_response, success_response
from integrations.authentication import (
    IntegrationJWTAuthentication,
    IntegrationTokenUser,
)

from .models import Farm, FarmIntegrationAccess
from .serializers import (
    FarmSyncBBoxSerializer,
    FarmSyncCentroidSerializer,
    FarmSyncSerializer,
)

logger = logging.getLogger(__name__)
User = get_user_model()
SERVICE_USERNAME = "nextcloud-integration"


def _integration_scopes(request: Request) -> set[str]:
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


def _enforce_integration_scope(request: Request) -> None:
    if not isinstance(request.user, IntegrationTokenUser):
        return
    scopes = _integration_scopes(request)
    if not scopes:
        raise PermissionDenied("Integration token scope missing.")
    if not scopes.intersection({"write", "admin"}):
        raise PermissionDenied("Integration token scope not permitted.")


farm_sync_error_schema = error_envelope_serializer("FarmSyncErrorResponse")
farm_sync_data_schema = inline_serializer(
    name="FarmSyncData",
    fields={
        "id": serializers.IntegerField(),
        "external_farm_id": serializers.UUIDField(),
        "external_user_id": serializers.CharField(),
        "name": serializers.CharField(),
        "slug": serializers.CharField(),
        "bbox": FarmSyncBBoxSerializer(),
        "centroid": FarmSyncCentroidSerializer(allow_null=True),
    },
)
farm_sync_success_schema = success_envelope_serializer(
    "FarmSyncSuccessResponse",
    data=farm_sync_data_schema,
)


class FarmSyncView(APIView):
    """Create or update a farm from an external system.

    Auth: IntegrationJWTAuthentication.
    Permissions: IsAuthenticated.
    Request body: FarmSyncSerializer.
    Response data: farm identifiers, name, slug, bbox, and centroid.
    """

    authentication_classes = (IntegrationJWTAuthentication,)
    permission_classes = (IsAuthenticated,)

    @extend_schema(
        auth=cast(list[str], [{"BearerAuth": []}]),
        request=FarmSyncSerializer,
        responses={
            200: farm_sync_success_schema,
            400: farm_sync_error_schema,
            401: farm_sync_error_schema,
            403: farm_sync_error_schema,
        },
    )
    def post(self, request: Request) -> Response:
        """Sync a farm using external identifiers.

        Inputs: external_farm_id, external_user_id, name, bbox, centroid.
        Output: success envelope with farm metadata.
        Side effects: creates/updates Farm + FarmIntegrationAccess.
        """

        _enforce_integration_scope(request)
        serializer = FarmSyncSerializer(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
        except ValidationError:
            self._log_sync_failed(request, serializer.initial_data)
            raise

        service_user = User.objects.filter(username=SERVICE_USERNAME).first()
        if service_user is None:
            self._log_sync_failed(request, serializer.validated_data)
            return error_response(
                "Service user missing.",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        client_id = getattr(request, "integration_client_id", None)
        if not client_id and isinstance(request.user, IntegrationTokenUser):
            client_id = request.user.client_id
            request.integration_client_id = client_id

        bbox = serializer.validated_data["bbox"]
        centroid = serializer.validated_data.get("centroid")

        defaults = {
            "owner": service_user,
            "external_user_id": serializer.validated_data["external_user_id"],
            "name": serializer.validated_data["name"],
            "bbox_south": bbox["south"],
            "bbox_west": bbox["west"],
            "bbox_north": bbox["north"],
            "bbox_east": bbox["east"],
            "centroid_lat": centroid["lat"] if centroid else None,
            "centroid_lon": centroid["lon"] if centroid else None,
        }

        try:
            farm, created = Farm.objects.update_or_create(
                external_farm_id=serializer.validated_data["external_farm_id"],
                defaults=defaults,
            )
        except IntegrityError:
            self._log_sync_failed(request, serializer.validated_data)
            raise

        if client_id:
            FarmIntegrationAccess.objects.update_or_create(
                farm=farm,
                client_id=str(client_id),
                defaults={"is_active": True},
            )

        payload: dict[str, JSONValue] = {
            "id": farm.id,
            "external_farm_id": str(farm.external_farm_id),
            "external_user_id": farm.external_user_id,
            "name": farm.name,
            "slug": farm.slug,
            "bbox": {
                "south": str(farm.bbox_south),
                "west": str(farm.bbox_west),
                "north": str(farm.bbox_north),
                "east": str(farm.bbox_east),
            },
            "centroid": (
                {
                    "lat": str(farm.centroid_lat),
                    "lon": str(farm.centroid_lon),
                }
                if farm.centroid_lat is not None
                and farm.centroid_lon is not None
                else None
            ),
        }

        event = "farm_sync_created" if created else "farm_sync_updated"
        logger.info(
            "%s external_farm_id=%s external_user_id=%s client_id=%s",
            event,
            farm.external_farm_id,
            farm.external_user_id,
            client_id,
        )
        return success_response(payload, message="Farm synced")

    def _log_sync_failed(
        self, request: Request, payload: dict[str, Any] | None
    ) -> None:
        client_id = getattr(request, "integration_client_id", None)
        if not client_id and isinstance(request.user, IntegrationTokenUser):
            client_id = request.user.client_id
        external_farm_id = None
        external_user_id = None
        if isinstance(payload, dict):
            external_farm_id = payload.get("external_farm_id")
            external_user_id = payload.get("external_user_id")
        logger.warning(
            "farm_sync_failed external_farm_id=%s external_user_id=%s "
            "client_id=%s",
            external_farm_id,
            external_user_id,
            client_id,
        )
