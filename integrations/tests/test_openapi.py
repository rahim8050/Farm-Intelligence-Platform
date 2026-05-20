from __future__ import annotations

from integrations.authentication import IntegrationJWTAuthentication
from integrations.openapi import IntegrationJWTAuthenticationScheme


def test_integration_jwt_openapi_security_definition() -> None:
    scheme = IntegrationJWTAuthenticationScheme(
        target=IntegrationJWTAuthentication()
    )
    definition = scheme.get_security_definition(auto_schema=None)
    assert definition == {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
    }
