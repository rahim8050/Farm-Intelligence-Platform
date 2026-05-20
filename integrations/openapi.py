"""drf-spectacular extensions for integration JWT authentication."""

from __future__ import annotations

from typing import Any

from drf_spectacular.extensions import OpenApiAuthenticationExtension


class IntegrationJWTAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = "integrations.authentication.IntegrationJWTAuthentication"
    name = "IntegrationJWTAuth"

    def get_security_definition(
        self,
        auto_schema: Any,
    ) -> dict[str, object]:
        return {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
        }
