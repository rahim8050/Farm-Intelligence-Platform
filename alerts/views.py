"""HTTP views for the ``alerts`` app.

Auth: every endpoint requires an authenticated user (or an admin,
for the manual broadcast endpoint). The subscription endpoints are
scoped to the caller's own rows; the alert list is the caller's own
history; ``acknowledge`` is the caller's own alert.

Response envelope: every successful response is wrapped by
``config.api.responses.success_response`` so the OpenAPI
documentation matches the rest of the project.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from alerts.models import AudioAlert, AudioAlertSubscription
from alerts.serializers import (
    AdminBroadcastSerializer,
    AudioAlertSerializer,
    AudioAlertSubscriptionSerializer,
)
from alerts.services import (
    acknowledge_alert,
    list_alerts_for_user,
)
from alerts.triggers import on_admin_broadcast
from config.api.openapi import (
    error_envelope_serializer,
    success_envelope_serializer,
)
from config.api.responses import success_response


def _user_id(request: Request) -> int:
    """Resolve ``request.user.id`` to ``int``.

    All views that call this are guarded by ``IsAuthenticated`` so the
    ``cast`` is safe at runtime. mypy still needs the explicit
    narrowing because ``request.user`` is ``User | AnonymousUser``.
    """
    user_id = request.user.id
    if not isinstance(user_id, int):  # pragma: no cover - defensive
        raise RuntimeError("unauthenticated request reached _user_id")
    return user_id


def _authed_user(request: Request) -> Any:
    """Return ``request.user`` narrowed to a real :class:`User` instance.

    Use this for query filters so mypy accepts the ``User | AnonymousUser``
    union. At runtime, ``IsAuthenticated`` has already rejected any
    unauthenticated caller.
    """
    user = request.user
    if not user.is_authenticated:  # pragma: no cover - defensive
        raise RuntimeError("unauthenticated request reached _authed_user")
    return user


_NullData = serializers.JSONField(allow_null=True)

SubscriptionEnvelope = success_envelope_serializer(
    "AlertSubscriptionEnvelope",
    data=AudioAlertSubscriptionSerializer(),
)
SubscriptionListEnvelope = success_envelope_serializer(
    "AlertSubscriptionListEnvelope",
    data=AudioAlertSubscriptionSerializer(many=True),
)
AlertListEnvelope = success_envelope_serializer(
    "AlertListEnvelope",
    data=AudioAlertSerializer(many=True),
)
AlertDetailEnvelope = success_envelope_serializer(
    "AlertDetailEnvelope",
    data=AudioAlertSerializer(),
)
AlertAckEnvelope = success_envelope_serializer(
    "AlertAckEnvelope", data=_NullData
)
AdminSendEnvelope = success_envelope_serializer(
    "AlertAdminSendEnvelope", data=_NullData
)
AlertErrorEnvelope = error_envelope_serializer("AlertErrorEnvelope")


@extend_schema(
    auth=cast(list[str], [{"BearerAuth": []}, {"ApiKeyAuth": []}]),
)
class SubscriptionListCreateView(APIView):
    """List or create the caller's audio-alert subscriptions.

    Auth: IsAuthenticated
    Throttle: inherits the global user throttle (no app-specific scope).
    Response data: a list of :class:`AudioAlertSubscriptionSerializer`
    (for ``GET``) or a single instance (for ``POST``).
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        responses={
            200: SubscriptionListEnvelope,
            401: AlertErrorEnvelope,
        },
        summary="List audio alert subscriptions",
        operation_id="v1_alerts_subscriptions_list",
    )
    def get(self, request: Request) -> Response:
        rows = list(
            AudioAlertSubscription.objects.filter(
                user=_authed_user(request)
            ).order_by("-updated_at")
        )
        data = AudioAlertSubscriptionSerializer(rows, many=True).data
        return success_response(data)

    @extend_schema(
        request=AudioAlertSubscriptionSerializer,
        responses={
            201: SubscriptionEnvelope,
            400: AlertErrorEnvelope,
            401: AlertErrorEnvelope,
        },
        summary="Create or update a subscription",
        operation_id="v1_alerts_subscriptions_create",
    )
    def post(self, request: Request) -> Response:
        ser = AudioAlertSubscriptionSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        sub, created = AudioAlertSubscription.objects.update_or_create(
            user=request.user,
            farm_id=ser.validated_data["farm"].id
            if hasattr(ser.validated_data["farm"], "id")
            else ser.validated_data["farm"],
            defaults={"alert_types": ser.validated_data["alert_types"]},
        )
        body = AudioAlertSubscriptionSerializer(sub).data
        return success_response(
            body, status_code=status.HTTP_201_CREATED if created else 200
        )


@extend_schema(
    auth=cast(list[str], [{"BearerAuth": []}, {"ApiKeyAuth": []}]),
)
class SubscriptionDetailView(APIView):
    """Update or delete a single subscription.

    Auth: IsAuthenticated
    Response data: the updated subscription (PATCH) or ``null`` (DELETE).
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=AudioAlertSubscriptionSerializer,
        responses={
            200: SubscriptionEnvelope,
            400: AlertErrorEnvelope,
            401: AlertErrorEnvelope,
            404: AlertErrorEnvelope,
        },
        summary="Update a subscription",
        operation_id="v1_alerts_subscriptions_update",
    )
    def patch(self, request: Request, sub_id: UUID) -> Response:
        try:
            sub = AudioAlertSubscription.objects.get(
                id=sub_id, user=_authed_user(request)
            )
        except AudioAlertSubscription.DoesNotExist:
            return success_response(None, message="Not found", status_code=404)
        ser = AudioAlertSubscriptionSerializer(sub, data=request.data)
        ser.is_valid(raise_exception=True)
        ser.save()
        return success_response(ser.data)

    @extend_schema(
        responses={
            200: AlertAckEnvelope,
            401: AlertErrorEnvelope,
            404: AlertErrorEnvelope,
        },
        summary="Delete a subscription",
        operation_id="v1_alerts_subscriptions_delete",
    )
    def delete(self, request: Request, sub_id: UUID) -> Response:
        deleted, _ = AudioAlertSubscription.objects.filter(
            id=sub_id, user=_authed_user(request)
        ).delete()
        if not deleted:
            return success_response(None, message="Not found", status_code=404)
        return success_response(None, message="Subscription removed")


@extend_schema(
    auth=cast(list[str], [{"BearerAuth": []}, {"ApiKeyAuth": []}]),
)
class AlertListView(APIView):
    """List the caller's audio alerts (newest first).

    Auth: IsAuthenticated
    Query params:
        - ``unread=true`` to filter to unacknowledged rows.
        - ``limit`` (1..500, default 100).
    Response data: a list of :class:`AudioAlertSerializer`.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        responses={
            200: AlertListEnvelope,
            401: AlertErrorEnvelope,
        },
        summary="List audio alerts",
        operation_id="v1_alerts_list",
    )
    def get(self, request: Request) -> Response:
        only_unack = request.query_params.get("unread", "").lower() in {
            "1",
            "true",
            "yes",
        }
        try:
            limit = int(request.query_params.get("limit", "100"))
        except ValueError:
            limit = 100
        rows = list_alerts_for_user(
            user_id=_user_id(request),
            only_unacknowledged=only_unack,
            limit=limit,
        )
        data = AudioAlertSerializer(
            rows, many=True, context={"request": request}
        ).data
        return success_response(data)


@extend_schema(
    auth=cast(list[str], [{"BearerAuth": []}, {"ApiKeyAuth": []}]),
)
class AlertDetailView(APIView):
    """Retrieve or acknowledge a single audio alert.

    Auth: IsAuthenticated
    GET response data: :class:`AudioAlertSerializer`.
    POST response data: ``null`` (acknowledge is a side-effect call).
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        responses={
            200: AlertDetailEnvelope,
            401: AlertErrorEnvelope,
            404: AlertErrorEnvelope,
        },
        summary="Get one audio alert",
        operation_id="v1_alerts_retrieve",
    )
    def get(self, request: Request, alert_id: UUID) -> Response:
        try:
            alert = AudioAlert.objects.get(
                id=alert_id, user=_authed_user(request)
            )
        except AudioAlert.DoesNotExist:
            return success_response(None, message="Not found", status_code=404)
        data = AudioAlertSerializer(alert, context={"request": request}).data
        return success_response(data)

    @extend_schema(
        responses={
            200: AlertAckEnvelope,
            401: AlertErrorEnvelope,
            404: AlertErrorEnvelope,
        },
        summary="Acknowledge an audio alert",
        operation_id="v1_alerts_acknowledge",
    )
    def post(self, request: Request, alert_id: UUID) -> Response:
        ok = acknowledge_alert(user_id=_user_id(request), alert_id=alert_id)
        if (
            not ok
            and not AudioAlert.objects.filter(
                id=alert_id, user=_authed_user(request)
            ).exists()
        ):
            return success_response(None, message="Not found", status_code=404)
        return success_response(None, message="Acknowledged")


@extend_schema(
    auth=cast(list[str], [{"BearerAuth": []}, {"ApiKeyAuth": []}]),
)
class AdminBroadcastView(APIView):
    """Send a manual audio broadcast to a list of users (admin only).

    Auth: IsAdminUser
    Throttle: ``alerts_admin: 30/min``.
    Body: :class:`AdminBroadcastSerializer` (``user_ids``, ``title``,
    ``message``, optional ``farm_id``).
    """

    permission_classes = [IsAdminUser]
    throttle_scope = "alerts_admin"

    @extend_schema(
        request=AdminBroadcastSerializer,
        responses={
            200: AdminSendEnvelope,
            400: AlertErrorEnvelope,
            401: AlertErrorEnvelope,
            403: AlertErrorEnvelope,
        },
        summary="Send a manual audio alert (admin)",
        operation_id="v1_alerts_admin_send",
    )
    def post(self, request: Request) -> Response:
        ser = AdminBroadcastSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data: dict[str, Any] = ser.validated_data
        n = on_admin_broadcast(
            recipients=data["user_ids"],
            title=data["title"],
            message=data["message"],
            farm_id=data.get("farm_id"),
        )
        return success_response({"dispatched": n})
