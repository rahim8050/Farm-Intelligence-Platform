from __future__ import annotations

import logging
from typing import Any, cast

from django.db.models import QuerySet
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import BaseSerializer
from rest_framework.viewsets import ModelViewSet

from .models import Farm
from .permissions import IsFarmOwner
from .serializers import FarmSerializer

logger = logging.getLogger(__name__)


class FarmViewSet(ModelViewSet):
    serializer_class = FarmSerializer
    permission_classes = [IsAuthenticated, IsFarmOwner]

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
        # Owner-only visibility
        user_id = getattr(self.request.user, "id", None)
        if user_id is None:
            return Farm.objects.none()
        return Farm.objects.filter(owner_id=cast(int, user_id)).order_by(
            "-created_at"
        )

    def perform_create(self, serializer: BaseSerializer[Farm]) -> None:
        # Prevents clients from spoofing owner
        serializer.save(owner=self.request.user)
