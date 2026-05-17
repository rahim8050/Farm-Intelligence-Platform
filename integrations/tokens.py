"""Helpers for minting integration JWT access tokens."""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from rest_framework_simplejwt.tokens import AccessToken


def mint_integration_access_token(
    user_id: str,
    scope: str,
    client_id: str = "",
) -> tuple[str, int]:
    """Mint an integration access token and return it with expiry seconds."""

    lifetime_minutes = int(
        getattr(settings, "INTEGRATION_JWT_ACCESS_MINUTES", 5)
    )
    lifetime = timedelta(minutes=lifetime_minutes)

    token = AccessToken()
    token.set_exp(lifetime=lifetime)
    subject = str(user_id)
    token["user_id"] = subject
    token["sub"] = subject
    token["scope"] = scope or "read"
    if client_id:
        token["client_id"] = client_id
    token["iss"] = settings.SIMPLE_JWT["ISSUER"]
    token["aud"] = settings.SIMPLE_JWT["AUDIENCE"]

    return str(token), int(lifetime.total_seconds())
