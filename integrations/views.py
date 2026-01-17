"""Integration endpoints (e.g., Nextcloud).

Authentication: endpoints may override global auth to support
service-to-service integration flows. Bootstrap endpoints use API key + HMAC to
mint short-lived integration JWTs; session endpoints may require JWT-only.
Global defaults remain JWT + API key.

Admin-only endpoints are provided for managing legacy integration clients;
HMAC verification uses INTEGRATION_HMAC_CLIENTS_JSON.

All successful responses from these endpoints use the project envelope produced
by `config.api.responses.success_response`:

    {"status": 0, "message": "<str>", "data": <object|null>, "errors": null}
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Iterable
from datetime import timedelta
from typing import Any, cast

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiTypes,
    extend_schema,
    inline_serializer,
)
from rest_framework import serializers, status
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.negotiation import BaseContentNegotiation
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.renderers import BaseRenderer, JSONRenderer
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.settings import api_settings
from rest_framework.throttling import BaseThrottle
from rest_framework.views import APIView
from rest_framework.viewsets import ModelViewSet
from rest_framework_simplejwt.authentication import JWTAuthentication

from api_keys.authentication import ApiKeyAuthentication
from api_keys.throttling import ApiKeyRateThrottle
from config.api.openapi import (
    error_envelope_serializer,
    success_envelope_serializer,
)
from config.api.responses import JSONValue, error_response, success_response

from .authentication import IntegrationJWTAuthentication
from .hmac import (
    INTEGRATIONS_CLIENT_ID_HEADER,
    INTEGRATIONS_NONCE_HEADER,
    INTEGRATIONS_SIGNATURE_HEADER,
    INTEGRATIONS_TIMESTAMP_HEADER,
    NEXTCLOUD_CLIENT_ID_HEADER,
    NEXTCLOUD_NONCE_HEADER,
    NEXTCLOUD_SIGNATURE_HEADER,
    NEXTCLOUD_TIMESTAMP_HEADER,
)
from .models import IntegrationClient
from .permissions import IntegrationHMACPermission, NextcloudHMACPermission
from .serializers import (
    IntegrationClientCreateSerializer,
    IntegrationClientSerializer,
    IntegrationClientUpdateSerializer,
    IntegrationTokenRequestSerializer,
)
from .throttling import NextcloudHMACRateThrottle
from .tokens import mint_integration_access_token

logger = logging.getLogger(__name__)

DEFAULT_THROTTLE_CLASSES: tuple[type[BaseThrottle], ...] = cast(
    tuple[type[BaseThrottle], ...],
    tuple(api_settings.DEFAULT_THROTTLE_CLASSES),
)

PREVIEW_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR4nGNgYGBgAAAABQABJzQnCgAAAABJRU5ErkJggg=="
)


class IgnoreAcceptHeaderNegotiation(BaseContentNegotiation):
    """Force JSON negotiation so Accept headers never block PNG errors."""

    def select_renderer(
        self,
        request: Request,
        renderers: Iterable[BaseRenderer],
        format_suffix: str | None = None,
    ) -> tuple[BaseRenderer, str]:
        renderer_list = list(renderers)
        renderer = next(
            (item for item in renderer_list if isinstance(item, JSONRenderer)),
            renderer_list[0],
        )
        return renderer, renderer.media_type


nextcloud_ping_data_schema = inline_serializer(
    name="NextcloudPingData",
    fields={
        "ok": serializers.BooleanField(),
        "client_id": serializers.CharField(),
    },
)
nextcloud_ping_success_schema = success_envelope_serializer(
    "NextcloudPingSuccessResponse",
    data=nextcloud_ping_data_schema,
)
nextcloud_ping_error_schema = error_envelope_serializer(
    "NextcloudPingErrorResponse"
)

nextcloud_status_capabilities_schema = inline_serializer(
    name="NextcloudStatusCapabilities",
    fields={
        "png_preview": serializers.BooleanField(),
    },
)
nextcloud_status_data_schema = inline_serializer(
    name="NextcloudStatusData",
    fields={
        "ok": serializers.BooleanField(),
        "server_time": serializers.DateTimeField(),
        "version": serializers.CharField(),
        "capabilities": nextcloud_status_capabilities_schema,
    },
)
nextcloud_status_success_schema = success_envelope_serializer(
    "NextcloudStatusSuccessResponse",
    data=nextcloud_status_data_schema,
)

integration_ping_data_schema = inline_serializer(
    name="IntegrationPingData",
    fields={
        "ok": serializers.BooleanField(),
        "auth": serializers.CharField(),
        "user_id": serializers.CharField(),
        "key_id": serializers.CharField(allow_null=True),
        "scope": serializers.CharField(allow_null=True),
        "server_time": serializers.DateTimeField(),
    },
)
integration_ping_success_schema = success_envelope_serializer(
    "IntegrationPingSuccessResponse",
    data=integration_ping_data_schema,
)

integration_token_data_schema = inline_serializer(
    name="IntegrationTokenData",
    fields={
        "access": serializers.CharField(),
        "token_type": serializers.CharField(),
        "expires_in": serializers.IntegerField(),
    },
)
integration_token_success_schema = success_envelope_serializer(
    "IntegrationTokenSuccessResponse",
    data=integration_token_data_schema,
)

integration_whoami_data_schema = inline_serializer(
    name="IntegrationWhoamiData",
    fields={
        "sub": serializers.CharField(),
        "scope": serializers.CharField(),
        "server_time": serializers.DateTimeField(),
    },
)
integration_whoami_success_schema = success_envelope_serializer(
    "IntegrationWhoamiSuccessResponse",
    data=integration_whoami_data_schema,
)

integration_auth_error_schema = error_envelope_serializer(
    "IntegrationAuthErrorResponse"
)

integration_client_create_data_schema = inline_serializer(
    name="IntegrationClientCreateData",
    fields={
        "id": serializers.UUIDField(),
        "name": serializers.CharField(),
        "client_id": serializers.UUIDField(),
        "client_secret": serializers.CharField(),
    },
)
integration_client_create_success_schema = success_envelope_serializer(
    "IntegrationClientCreateSuccessResponse",
    data=integration_client_create_data_schema,
)
integration_client_rotate_data_schema = inline_serializer(
    name="IntegrationClientRotateSecretData",
    fields={
        "client_id": serializers.UUIDField(),
        "client_secret": serializers.CharField(),
        "previous_valid_until": serializers.DateTimeField(allow_null=True),
    },
)
integration_client_rotate_success_schema = success_envelope_serializer(
    "IntegrationClientRotateSecretSuccessResponse",
    data=integration_client_rotate_data_schema,
)
integration_client_list_success_schema = success_envelope_serializer(
    "IntegrationClientListSuccessResponse",
    data=IntegrationClientSerializer(many=True),
)
integration_client_retrieve_success_schema = success_envelope_serializer(
    "IntegrationClientRetrieveSuccessResponse",
    data=IntegrationClientSerializer(),
)
integration_client_update_success_schema = success_envelope_serializer(
    "IntegrationClientUpdateSuccessResponse",
    data=IntegrationClientSerializer(),
)
integration_client_error_schema = error_envelope_serializer(
    "IntegrationClientErrorResponse"
)
integration_client_conflict_schema = inline_serializer(
    name="IntegrationClientConflictResponse",
    fields={
        "status": serializers.IntegerField(),
        "message": serializers.CharField(),
        "data": serializers.JSONField(allow_null=True),
        "errors": serializers.JSONField(allow_null=True),
    },
)


@extend_schema(auth=[])
class NextcloudPingView(APIView):
    """Verify Nextcloud request signing (HMAC) configuration.

    Authentication: none (overrides global JWT/API key auth).
    Permissions: `NextcloudHMACPermission` only.
    Throttling: NextcloudHMACRateThrottle + global DRF throttles.
    Response data: `{"ok": true, "client_id": "<client_id>"}`.
    """

    authentication_classes = ()
    permission_classes = (NextcloudHMACPermission,)
    throttle_classes = (NextcloudHMACRateThrottle,) + DEFAULT_THROTTLE_CLASSES

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name=INTEGRATIONS_CLIENT_ID_HEADER,
                type=OpenApiTypes.STR,
                location=OpenApiParameter.HEADER,
                required=True,
                description="Integration client identifier (UUID).",
            ),
            OpenApiParameter(
                name=NEXTCLOUD_CLIENT_ID_HEADER,
                type=OpenApiTypes.STR,
                location=OpenApiParameter.HEADER,
                required=False,
                deprecated=True,
                description="Deprecated alias for X-Client-Id.",
            ),
            OpenApiParameter(
                name=NEXTCLOUD_TIMESTAMP_HEADER,
                type=OpenApiTypes.INT,
                location=OpenApiParameter.HEADER,
                required=True,
                description="Unix timestamp (seconds).",
            ),
            OpenApiParameter(
                name=NEXTCLOUD_NONCE_HEADER,
                type=OpenApiTypes.STR,
                location=OpenApiParameter.HEADER,
                required=True,
                description="Unique per-request nonce.",
            ),
            OpenApiParameter(
                name=NEXTCLOUD_SIGNATURE_HEADER,
                type=OpenApiTypes.STR,
                location=OpenApiParameter.HEADER,
                required=True,
                description="Hex HMAC-SHA256 of canonical request string.",
            ),
        ],
        responses={
            200: nextcloud_ping_success_schema,
            403: nextcloud_ping_error_schema,
        },
    )
    def get(self, request: Request) -> Response:  # type: ignore[override]
        """Return `ok` + verified `client_id` if HMAC validation succeeds.

        Inputs: signature headers (X-Client-Id or X-NC-CLIENT-ID,
        X-NC-TIMESTAMP, X-NC-NONCE, X-NC-SIGNATURE).
        Output: success envelope with `data.ok` and `data.client_id`.
        Side effects: stores the nonce in the configured cache for replay
        protection.
        """

        client_id = getattr(request, "nc_hmac_client_id", "")
        return success_response({"ok": True, "client_id": client_id})


@extend_schema(auth=cast(list[str], [{"BearerAuth": []}]))
class NextcloudStatusView(APIView):
    """Return a signed-in integration status payload for diagnostics.

    Authentication: BearerAuth (integration JWT).
    Permissions: IsAuthenticated.
    Request body: none.
    Response data: ok, server_time, version, capabilities.
    """

    authentication_classes = (IntegrationJWTAuthentication,)
    permission_classes = (IsAuthenticated,)
    renderer_classes = [JSONRenderer]
    content_negotiation_class = IgnoreAcceptHeaderNegotiation

    @extend_schema(
        responses={
            200: nextcloud_status_success_schema,
            401: integration_auth_error_schema,
            403: integration_auth_error_schema,
        }
    )
    def get(self, request: Request) -> Response:
        """Inputs: Authorization header (Bearer token).

        Output: success envelope with status metadata for diagnostics.
        Side effects: none.
        """

        version = str(settings.SPECTACULAR_SETTINGS.get("VERSION", ""))
        data: dict[str, JSONValue] = {
            "ok": True,
            "server_time": timezone.now().isoformat(),
            "version": version,
            "capabilities": {"png_preview": True},
        }
        return success_response(data, message="Integration status")


@extend_schema(auth=cast(list[str], [{"BearerAuth": []}]))
class NextcloudPreviewView(APIView):
    """Serve a tiny PNG preview for diagnostics.

    Authentication: BearerAuth (integration JWT).
    Permissions: IsAuthenticated.
    Request body: none.
    Response: image/png with Cache-Control: no-store.
    """

    authentication_classes = (IntegrationJWTAuthentication,)
    permission_classes = (IsAuthenticated,)
    renderer_classes = [JSONRenderer]
    content_negotiation_class = IgnoreAcceptHeaderNegotiation

    @extend_schema(
        responses={
            (200, "image/png"): OpenApiTypes.BINARY,
            401: integration_auth_error_schema,
            403: integration_auth_error_schema,
        }
    )
    def get(self, request: Request) -> HttpResponse:
        """Inputs: Authorization header (Bearer token).

        Output: image/png response with Cache-Control: no-store.
        Side effects: none.
        """

        response = HttpResponse(PREVIEW_PNG_BYTES, content_type="image/png")
        response["Cache-Control"] = "no-store"
        return response


class IntegrationPingView(APIView):
    """Authenticated ping endpoint for service-to-service integrations.

    Authentication: ApiKeyAuth (`X-API-Key`).
    Permissions: IsAuthenticated.
    Throttling: ApiKeyRateThrottle ("api_key").
    Response data: includes authenticated user and key metadata.
    """

    authentication_classes = (ApiKeyAuthentication,)
    permission_classes = (IsAuthenticated,)
    throttle_classes = (ApiKeyRateThrottle,)

    @extend_schema(
        responses={
            200: integration_ping_success_schema,
            401: integration_client_error_schema,
        }
    )
    def get(self, request: Request) -> Response:
        """Return `pong` with API key-authenticated metadata."""

        auth_obj = request.auth
        key_id = getattr(auth_obj, "id", None)
        scope = getattr(auth_obj, "scope", None)

        data: dict[str, Any] = {
            "ok": True,
            "auth": "api_key",
            "user_id": str(request.user.pk),
            "key_id": str(key_id) if key_id is not None else None,
            "scope": str(scope) if scope is not None else None,
            "server_time": timezone.now().isoformat(),
        }
        return success_response(data, message="pong")


@extend_schema(auth=cast(list[str], [{"ApiKeyAuth": []}]))
class IntegrationTokenView(APIView):
    """Mint a short-lived integration JWT via API key + HMAC bootstrap.

    Authentication: ApiKeyAuth (`X-API-Key`).
    Permissions: IsAuthenticated + IntegrationHMACPermission.
    Throttling: ApiKeyRateThrottle ("api_key") + NextcloudHMACRateThrottle.
    Request body: none.
    Response data: access token, token_type "Bearer", and expires_in seconds.
    """

    authentication_classes = (ApiKeyAuthentication,)
    permission_classes = (IsAuthenticated, IntegrationHMACPermission)
    throttle_classes = (ApiKeyRateThrottle, NextcloudHMACRateThrottle)

    @extend_schema(
        request=IntegrationTokenRequestSerializer,
        parameters=[
            OpenApiParameter(
                name=INTEGRATIONS_CLIENT_ID_HEADER,
                type=OpenApiTypes.STR,
                location=OpenApiParameter.HEADER,
                required=True,
                description="Integration client identifier (UUID).",
            ),
            OpenApiParameter(
                name=INTEGRATIONS_TIMESTAMP_HEADER,
                type=OpenApiTypes.INT,
                location=OpenApiParameter.HEADER,
                required=True,
                description="Unix timestamp (seconds).",
            ),
            OpenApiParameter(
                name=INTEGRATIONS_NONCE_HEADER,
                type=OpenApiTypes.STR,
                location=OpenApiParameter.HEADER,
                required=True,
                description="Unique per-request nonce.",
            ),
            OpenApiParameter(
                name=INTEGRATIONS_SIGNATURE_HEADER,
                type=OpenApiTypes.STR,
                location=OpenApiParameter.HEADER,
                required=True,
                description="Hex HMAC-SHA256 of canonical request string.",
            ),
        ],
        responses={
            200: integration_token_success_schema,
            401: integration_auth_error_schema,
            403: integration_auth_error_schema,
        },
    )
    def post(self, request: Request) -> Response:
        """Validate API key + HMAC headers, mint a JWT, and return it.

        Inputs: `X-API-Key` plus HMAC headers (`X-Client-Id`, `X-Timestamp`,
        `X-Nonce`, `X-Signature`).
        Output: success envelope with `data.access`, `data.token_type`,
        `data.expires_in`.
        Side effects: stores the nonce in cache for replay protection.
        """

        serializer = IntegrationTokenRequestSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)

        auth_obj = request.auth
        user_id = str(getattr(request.user, "pk", ""))
        scope = str(getattr(auth_obj, "scope", ""))

        access, expires_in = mint_integration_access_token(
            user_id=user_id,
            scope=scope,
        )
        data: dict[str, Any] = {
            "access": access,
            "token_type": "Bearer",
            "expires_in": expires_in,
        }
        return success_response(data, message="Integration token issued")


@extend_schema(auth=cast(list[str], [{"BearerAuth": []}]))
class IntegrationWhoAmIView(APIView):
    """Return the integration JWT identity claims.

    Authentication: BearerAuth (integration JWT).
    Permissions: IsAuthenticated.
    Request body: none.
    Response data: `sub`, `scope`, and `server_time`.
    """

    authentication_classes = (IntegrationJWTAuthentication,)
    permission_classes = (IsAuthenticated,)

    @extend_schema(
        responses={
            200: integration_whoami_success_schema,
            401: integration_auth_error_schema,
        }
    )
    def get(self, request: Request) -> Response:
        auth_obj: Any = getattr(request, "auth", None)

        def claim(key: str) -> str:
            # dict-like (your IntegrationJWTAuthentication could set this)
            if isinstance(auth_obj, dict):
                return str(auth_obj.get(key, "") or "")
            if auth_obj is None:
                return ""
            getter = getattr(auth_obj, "get", None)
            if callable(getter):
                try:
                    value = getter(key)
                except Exception:
                    value = None
                else:
                    if value is not None:
                        return str(value or "")
            # SimpleJWT Token objects support indexing: token["sub"]
            try:
                return str(auth_obj[key] or "")
            except Exception:
                return ""

        sub = (
            claim("sub")
            or claim("user_id")
            or str(getattr(request.user, "id", ""))
        )
        scope = claim("scope")

        # If scope isn't in the token, infer from authenticator
        if not scope:
            authn = getattr(request, "successful_authenticator", None)
            scope = (
                "api_key" if isinstance(authn, ApiKeyAuthentication) else "jwt"
            )

        data: dict[str, JSONValue] = {
            "sub": sub,
            "scope": scope,
            "server_time": timezone.now().isoformat(),
        }
        return success_response(data, message="Integration identity")


class IntegrationClientViewSet(ModelViewSet):
    """Admin CRUD for HMAC integration clients.

    Authentication: BearerAuth (JWT).
    Permissions: IsAdminUser.
    Secrets: never included in list/retrieve/update responses.
    """

    authentication_classes = (JWTAuthentication,)
    permission_classes = [IsAdminUser]
    queryset = IntegrationClient.objects.all()
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_serializer_class(self) -> type[serializers.Serializer]:
        if self.action in ("create",):
            return IntegrationClientCreateSerializer
        if self.action in ("update", "partial_update"):
            return IntegrationClientUpdateSerializer
        return IntegrationClientSerializer

    @extend_schema(
        request=IntegrationClientCreateSerializer,
        responses={
            201: integration_client_create_success_schema,
            400: integration_client_error_schema,
            401: integration_client_error_schema,
            403: integration_client_error_schema,
        },
    )
    def create(
        self,
        request: Request,
        *args: object,
        **kwargs: object,
    ) -> Response:
        """Create an IntegrationClient and return its secret once."""

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        client_secret = IntegrationClient.generate_secret()
        client = IntegrationClient.objects.create(
            name=serializer.validated_data["name"],
            is_active=serializer.validated_data.get("is_active", True),
            secret=client_secret,
        )

        data: dict[str, JSONValue] = {
            "id": str(client.id),
            "name": client.name,
            "client_id": str(client.client_id),
            "client_secret": client_secret,
        }
        return success_response(
            data,
            message="Integration client created",
            status_code=status.HTTP_201_CREATED,
        )

    @extend_schema(
        responses={
            200: integration_client_list_success_schema,
            401: integration_client_error_schema,
            403: integration_client_error_schema,
        }
    )
    def list(
        self,
        request: Request,
        *args: object,
        **kwargs: object,
    ) -> Response:
        """List IntegrationClients (no secret fields)."""

        queryset = self.filter_queryset(self.get_queryset().order_by("name"))
        serializer = self.get_serializer(queryset, many=True)
        return success_response(serializer.data, message="Integration clients")

    @extend_schema(
        responses={
            200: integration_client_retrieve_success_schema,
            401: integration_client_error_schema,
            403: integration_client_error_schema,
            404: integration_client_error_schema,
        }
    )
    def retrieve(
        self,
        request: Request,
        *args: object,
        **kwargs: object,
    ) -> Response:
        """Retrieve an IntegrationClient (no secret fields)."""

        instance = self.get_object()
        serializer = self.get_serializer(instance)
        return success_response(serializer.data, message="Integration client")

    @extend_schema(
        request=IntegrationClientUpdateSerializer,
        responses={
            200: integration_client_update_success_schema,
            400: integration_client_error_schema,
            401: integration_client_error_schema,
            403: integration_client_error_schema,
            404: integration_client_error_schema,
        },
    )
    def partial_update(
        self,
        request: Request,
        *args: object,
        **kwargs: object,
    ) -> Response:
        """Update `name`/`is_active` (secrets cannot be changed here)."""

        instance = self.get_object()
        serializer = self.get_serializer(
            instance,
            data=request.data,
            partial=True,
        )
        serializer.is_valid(raise_exception=True)
        client = serializer.save()
        out = IntegrationClientSerializer(client).data
        return success_response(out, message="Integration client updated")

    @extend_schema(
        request=None,
        responses={
            200: integration_client_rotate_success_schema,
            401: integration_client_error_schema,
            403: integration_client_error_schema,
            404: integration_client_error_schema,
            409: integration_client_conflict_schema,
        },
    )
    @action(
        detail=True,
        methods=["post"],
        url_path="rotate-secret",
    )
    def rotate_secret(
        self,
        request: Request,
        *args: object,
        **kwargs: object,
    ) -> Response:
        """Rotate the client's secret and return the new secret once."""

        client = self.get_object()

        if not client.is_active:
            return error_response(
                "Integration client is disabled",
                status_code=status.HTTP_409_CONFLICT,
            )

        overlap_seconds = int(
            getattr(settings, "INTEGRATIONS_HMAC_PREVIOUS_TTL_SECONDS", 259200)
        )
        overlap_ttl = timedelta(seconds=overlap_seconds)

        with transaction.atomic():
            locked_client = IntegrationClient.objects.select_for_update().get(
                pk=client.pk
            )
            if not locked_client.is_active:
                return error_response(
                    "Integration client is disabled",
                    status_code=status.HTTP_409_CONFLICT,
                )
            client_secret = locked_client.rotate_secret(
                overlap_ttl=overlap_ttl
            )
            locked_client.save(
                update_fields=[
                    "secret",
                    "previous_secret",
                    "previous_expires_at",
                    "rotated_at",
                    "updated_at",
                ]
            )
            client = locked_client

        logger.info(
            "integration_client.secret_rotated "
            "client_id=%s integration_client_id=%s",
            client.client_id,
            client.id,
        )

        data: dict[str, JSONValue] = {
            "client_id": str(client.client_id),
            "client_secret": client_secret,
            "previous_valid_until": (
                client.previous_expires_at.isoformat()
                if client.previous_expires_at
                else None
            ),
        }
        return success_response(data, message="Secret rotated")


@api_view(["GET"])
@permission_classes(
    [AllowAny]
)  # TEMP: see headers presence; revert after debugging
def debug_ping_headers(request: Request) -> Response:
    want = [
        "X-Client-Id",
        "X-NC-CLIENT-ID",
        "X-NC-TIMESTAMP",
        "X-NC-NONCE",
        "X-NC-SIGNATURE",
    ]
    got = {k: (request.headers.get(k) is not None) for k in want}
    logger.warning("PING header presence: %s", got)
    return Response({"ok": True, "headers_present": got})
