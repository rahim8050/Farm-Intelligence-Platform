"""DRF permissions for Nextcloud HMAC request signing.

These permissions are designed to be composed with existing JWT and API key
auth without changing the global authentication stack.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission
from rest_framework.request import Request
from rest_framework.views import APIView

from .hmac import (
    INTEGRATIONS_CLIENT_ID_HEADER,
    NEXTCLOUD_CLIENT_ID_HEADER,
    NextcloudHMACVerificationError,
    verify_nextcloud_hmac_request,
)

INTEGRATION_HMAC_NONCE_TTL_SECONDS = 600

logger = logging.getLogger(__name__)


def _request_id(request: Request) -> str:
    request_id = request.headers.get("X-Request-ID")
    if not request_id:
        request_id = request.headers.get("X-Request-Id", "")
    return request_id


def _log_failure(
    request: Request,
    exc: NextcloudHMACVerificationError,
) -> None:
    request_id = _request_id(request) or "unknown"
    client_id = request.headers.get(INTEGRATIONS_CLIENT_ID_HEADER)
    if not client_id:
        client_id = request.headers.get(NEXTCLOUD_CLIENT_ID_HEADER, "")
    remote_addr = request.META.get("REMOTE_ADDR", "unknown")
    user_agent = request.headers.get("User-Agent") or "unknown"
    logger.warning(
        "nextcloud_hmac.denied code=%s path=%s method=%s request_id=%s "
        "client_id=%s remote_addr=%s user_agent=%s",
        exc.code,
        request.path,
        request.method,
        request_id,
        client_id or "unknown",
        remote_addr,
        user_agent,
    )


def _permission_detail(
    exc: NextcloudHMACVerificationError,
) -> dict[str, str]:
    return {
        "detail": "Invalid Nextcloud signature",
        "code": exc.code,
        "reason": str(exc),
    }


class NextcloudHMACPermission(BasePermission):
    """Require a valid Nextcloud instance HMAC signature.

    When valid, sets `request.nc_hmac_client_id` for downstream use.
    """

    def has_permission(self, request: Request, view: APIView) -> bool:
        try:
            allowed_methods = getattr(view, "allowed_methods", None)
            client_id = verify_nextcloud_hmac_request(
                request,
                allowed_methods=allowed_methods,
            )
        except NextcloudHMACVerificationError as exc:
            _log_failure(request, exc)
            raise PermissionDenied(_permission_detail(exc)) from exc

        cast(Any, request).nc_hmac_client_id = client_id
        return True


class IntegrationHMACPermission(BasePermission):
    """Require a valid integration HMAC signature for token bootstrap.

    When valid, sets `request.nc_hmac_client_id` for downstream use.
    """

    def has_permission(self, request: Request, view: APIView) -> bool:
        try:
            allowed_methods = getattr(view, "allowed_methods", None)
            client_id = verify_nextcloud_hmac_request(
                request,
                nonce_ttl_seconds=INTEGRATION_HMAC_NONCE_TTL_SECONDS,
                allowed_methods=allowed_methods,
            )
        except NextcloudHMACVerificationError as exc:
            _log_failure(request, exc)
            raise PermissionDenied(_permission_detail(exc)) from exc

        cast(Any, request).nc_hmac_client_id = client_id
        return True
