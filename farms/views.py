from __future__ import annotations

import logging
from typing import Any, cast

from django.db.models import Q, QuerySet
from django.http import Http404
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import BaseSerializer
from rest_framework.viewsets import ModelViewSet
from rest_framework_simplejwt.authentication import JWTAuthentication

from api_keys.authentication import ApiKeyAuthentication
from integrations.authentication import IntegrationJWTAuthentication

from .models import Farm
from .permissions import IsFarmOwner
from .serializers import FarmSerializer

logger = logging.getLogger(__name__)


def _auth_type(request: Request) -> str:
    api_key = request.META.get("HTTP_X_API_KEY")
    if api_key:
        return "api_key"
    auth_header = request.META.get("HTTP_AUTHORIZATION", "")
    if isinstance(auth_header, str) and auth_header.lower().startswith(
        "bearer "
    ):
        return "jwt_bearer"
    if auth_header:
        return "authorization"
    return "unknown"


class FarmViewSet(ModelViewSet):
    serializer_class = FarmSerializer
    permission_classes = [IsAuthenticated, IsFarmOwner]
    authentication_classes = [
        IntegrationJWTAuthentication,
        JWTAuthentication,
        ApiKeyAuthentication,
    ]

    def finalize_response(
        self,
        request: Request,
        response: Response,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        response = super().finalize_response(
            request, response, *args, **kwargs
        )
        request_id = (
            request.headers.get("X-Request-Id")
            or request.META.get("HTTP_X_REQUEST_ID")
            or "-"
        )
        logger.info(
            "farms request: method=%s path=%s status=%s request_id=%s",
            request.method,
            request.path,
            response.status_code,
            request_id,
        )
        return response

    def get_queryset(self) -> QuerySet[Farm]:
        from integrations.authentication import IntegrationTokenUser

        user = self.request.user
        if isinstance(user, IntegrationTokenUser):
            client_id = user.client_id
            return (
                Farm.objects.filter(
                    Q(integration_access__client_id=client_id)
                    & Q(integration_access__is_active=True)
                )
                .distinct()
                .order_by("-created_at")
            )

        user_id = getattr(user, "id", None)
        if user_id is None:
            return Farm.objects.none()
        return (
            Farm.objects.filter(
                Q(owner_id=cast(int, user_id))
                | Q(
                    integration_access__is_active=True,
                )
            )
            .distinct()
            .order_by("-created_at")
        )

    def get_object(self) -> Farm:
        try:
            return super().get_object()
        except Http404:
            request = self.request
            lookup_field = self.lookup_field or "pk"
            farm_id = self.kwargs.get(lookup_field)
            logger.debug(
                "farms.not_found farm_id=%s user_id=%s auth=%s path=%s",
                farm_id,
                getattr(request.user, "id", None),
                _auth_type(request),
                getattr(request, "path", ""),
            )
            raise

    def perform_create(self, serializer: BaseSerializer[Farm]) -> None:
        # Prevents clients from spoofing owner
        serializer.save(owner=self.request.user)
