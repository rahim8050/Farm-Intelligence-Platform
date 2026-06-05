"""drf-spectacular helpers for documenting the project's response envelopes.

The runtime response helpers in `config.api.responses` and the global DRF
exception handler wrap nearly all API responses in a consistent JSON envelope.
These utilities generate matching serializers for OpenAPI documentation without
changing runtime behavior.
"""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import inline_serializer
from rest_framework import serializers
from rest_framework.serializers import Serializer


def success_envelope_serializer(
    name: str,
    *,
    data: serializers.Field,
) -> Serializer:
    """Build an OpenAPI schema matching `success_response`."""

    return inline_serializer(
        name=name,
        fields={
            "status": serializers.IntegerField(),
            "message": serializers.CharField(),
            "data": data,
            "errors": serializers.JSONField(allow_null=True),
            "request_id": serializers.CharField(allow_null=True),
        },
    )


def error_envelope_serializer(name: str) -> Serializer:
    """Build an OpenAPI schema matching `custom_exception_handler`."""

    return inline_serializer(
        name=name,
        fields={
            "status": serializers.IntegerField(),
            "message": serializers.CharField(),
            "errors": serializers.JSONField(allow_null=True),
            "request_id": serializers.CharField(allow_null=True),
        },
    )


def remove_deprecated_integration_aliases(
    result: dict[str, Any],
    generator: Any,
    request: Any,
    public: bool,
) -> dict[str, Any]:
    """Strip deprecated integration alias routes from the schema output."""

    paths = result.get("paths")
    if not isinstance(paths, dict):
        return result

    deprecated_prefixes = (
        "/api/v1/integration/",
        "/api/v1/integrations/integrations/",
    )
    for path in list(paths.keys()):
        if path.startswith(deprecated_prefixes):
            paths.pop(path, None)

    return result
