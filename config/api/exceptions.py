from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rest_framework.response import Response


type JSONValue = (
    None
    | bool
    | int
    | float
    | str
    | list[JSONValue]
    | dict[str, JSONValue]
)


def _to_json_value(value: object) -> JSONValue:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return {str(k): _to_json_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_to_json_value(v) for v in value]
    return str(value)


def custom_exception_handler(
    exc: Exception,
    context: dict[str, Any],
) -> Response:
    # Lazy imports: safe even if settings aren't configured at import time.
    from rest_framework import status
    from rest_framework.exceptions import Throttled
    from rest_framework.response import Response
    from rest_framework.views import exception_handler as drf_exception_handler

    response = drf_exception_handler(exc, context)

    if response is None:
        return Response(
            {"status": 1, "message": "Internal server error", "errors": None},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    if isinstance(exc, Throttled):
        detail = _to_json_value(response.data)
        errors: dict[str, JSONValue]
        if isinstance(detail, dict):
            errors = {**detail}
        else:
            errors = {"detail": detail}

        wait = getattr(exc, "wait", None)
        if wait is not None:
            errors["wait"] = wait

        response.data = {
            "status": 1,
            "message": "Too Many Requests",
            "data": None,
            "errors": errors,
        }
        return response

    detail = _to_json_value(response.data)
    message = "Request failed"
    if isinstance(detail, dict):
        maybe = detail.get("detail")
        if isinstance(maybe, str):
            message = maybe

    response.data = {"status": 1, "message": message, "errors": detail}
    return response
