"""drf-spectacular extensions for farm observation authentication."""

from __future__ import annotations

from typing import Any

from drf_spectacular.extensions import OpenApiAuthenticationExtension


class FarmObservationAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = "farms.authentication.FarmObservationAuthentication"
    name = "FarmObservationAuth"

    def get_security_definition(
        self,
        auto_schema: Any,
    ) -> dict[str, object]:
        return {
            "oneOf": [
                {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-API-Key",
                },
                {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "JWT",
                },
            ]
        }
