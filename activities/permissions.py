"""Activity scheduling permissions.

Supports owner-based and integration-based access control.
Integration access: allow-listed per farm via FarmIntegrationAccess.
"""

from __future__ import annotations

from typing import Any

from django.http import Http404
from rest_framework.permissions import BasePermission
from rest_framework.request import Request
from rest_framework.views import APIView

from integrations.authentication import IntegrationTokenUser

from .models import Activity


class IsActivityOwner(BasePermission):
    """Check if user owns the activity."""

    def has_object_permission(
        self, request: Request, view: APIView, obj: object
    ) -> bool:
        if not isinstance(obj, Activity):
            return False
        return bool(
            request.user and obj.owner_id == getattr(request.user, "id", None)
        )


class ActivityIntegrationMixin:
    """Mixin to support integration JWT access to activities."""

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

    def get_object(self) -> Activity:
        user = self.request.user

        if isinstance(user, IntegrationTokenUser):
            lookup_field = self.lookup_field or "pk"
            farm_id = self.kwargs.get(lookup_field)

            try:
                return Activity.objects.select_related("farm", "owner").get(
                    farm_id=farm_id,
                    farm__is_active=True,
                    farm__integration_access__client_id=user.client_id,
                    farm__integration_access__is_active=True,
                )
            except Activity.DoesNotExist as exc:
                raise Http404 from exc

        return super().get_object()
