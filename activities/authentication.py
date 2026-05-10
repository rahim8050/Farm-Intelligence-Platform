"""Activity scheduling authentication.

Supports API key, user JWT, or integration JWT authentication.
Integration access: allow-listed per farm via FarmIntegrationAccess.
"""

from __future__ import annotations

from rest_framework.authentication import BaseAuthentication
from rest_framework.request import Request
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken

from api_keys.auth import get_header_key
from api_keys.authentication import ApiKeyAuthentication
from integrations.authentication import IntegrationJWTAuthentication


def _is_missing_scope(exc: InvalidToken) -> bool:
    for arg in exc.args:
        if isinstance(arg, str) and "scope" in arg.lower():
            return True
    return False


class ActivityAuthentication(BaseAuthentication):
    """Authenticate API key, integration JWT, or user JWT credentials."""

    def __init__(self) -> None:
        super().__init__()
        self._api_key_auth = ApiKeyAuthentication()
        self._integration_auth = IntegrationJWTAuthentication()
        self._jwt_auth = JWTAuthentication()

    def authenticate(self, request: Request) -> tuple[object, object] | None:
        if get_header_key(request) is not None:
            return self._api_key_auth.authenticate(request)

        header = self._jwt_auth.get_header(request)
        if header is None:
            return None

        raw_token = self._jwt_auth.get_raw_token(header)
        if raw_token is None:
            return None

        try:
            return self._integration_auth.authenticate(request)
        except InvalidToken as exc:
            if not _is_missing_scope(exc):
                raise

        return self._jwt_auth.authenticate(request)
