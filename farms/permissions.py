from __future__ import annotations

from rest_framework.permissions import BasePermission
from rest_framework.request import Request
from rest_framework.views import APIView

from .models import Farm


class IsFarmOwner(BasePermission):
    def has_object_permission(
        self, request: Request, view: APIView, obj: object
    ) -> bool:
        if not isinstance(obj, Farm):
            return False
        user_id = getattr(request.user, "id", None)
        if user_id is not None and obj.owner_id == user_id:
            return True
        from integrations.authentication import IntegrationTokenUser

        if isinstance(request.user, IntegrationTokenUser):
            client_id = request.user.client_id
            return bool(
                obj.integration_access.filter(
                    client_id=client_id, is_active=True
                ).exists()
            )
        return bool(obj.integration_access.filter(is_active=True).exists())
