from __future__ import annotations

from rest_framework import status
from rest_framework.response import Response

from config.api.request_id import current_request_id

type JSONScalar = str | int | float | bool | None
type JSONValue = JSONScalar | list[JSONValue] | dict[str, JSONValue]


def success_response(
    data: JSONValue | None,
    message: str = "OK",
    *,
    status_code: int = status.HTTP_200_OK,
) -> Response:
    payload: dict[str, JSONValue] = {
        "status": 0,
        "message": message,
        "data": data,
        "errors": None,
        "request_id": current_request_id() or None,
    }
    return Response(payload, status=status_code)


def error_response(
    message: str,
    *,
    errors: JSONValue | None = None,
    status_code: int = status.HTTP_400_BAD_REQUEST,
) -> Response:
    payload: dict[str, JSONValue] = {
        "status": 1,
        "message": message,
        "data": None,
        "errors": errors,
        "request_id": current_request_id() or None,
    }
    return Response(payload, status=status_code)
