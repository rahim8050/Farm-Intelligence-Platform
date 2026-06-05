"""Request / correlation ID propagation.

Per the production-readiness review
(``prompts/p4-staff-engineer-review.md`` #10), every request gets a
``X-Request-ID`` (read from the incoming header, or generated as a
UUIDv4 if missing). The ID is exposed as ``request.request_id``,
stamped on the response, and pushed into a ``contextvars`` slot
that the Celery ``before_task`` hook and the structured log
formatter pick up.

The helper functions :func:`current_request_id` and
:func:`bind_request_id` are the only public surface area besides
:class:`RequestIdMiddleware`; everything else is internal.
"""

from __future__ import annotations

import contextvars
import uuid
from collections.abc import Callable
from typing import Any

_request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default=""
)


def current_request_id() -> str:
    """Return the active request / correlation id (empty if none)."""
    return _request_id_var.get()


def bind_request_id(value: str) -> contextvars.Token[str]:
    """Set the active request id and return a token for restoration.

    Use this from Celery ``before_task`` hooks, WebSocket consumers,
    and any other async entry point that doesn't go through
    :class:`RequestIdMiddleware`.
    """
    return _request_id_var.set(value)


def reset_request_id(token: contextvars.Token[str]) -> None:
    """Restore the request id to its previous value (pair with
    :func:`bind_request_id`)."""
    _request_id_var.reset(token)


def new_request_id() -> str:
    """Generate a fresh UUIDv4 request id."""
    return uuid.uuid4().hex


HEADER_NAME = "HTTP_X_REQUEST_ID"
RESPONSE_HEADER = "X-Request-ID"


class RequestIdMiddleware:
    """Read or generate the request id and stamp it on the response.

    Place near the top of ``MIDDLEWARE`` so every request gets an
    id, including those rejected by auth or throttling.
    """

    def __init__(self, get_response: Callable[..., Any]) -> None:
        self.get_response = get_response

    def __call__(self, request: Any) -> Any:
        incoming = request.META.get(HEADER_NAME, "").strip()
        rid = incoming or new_request_id()
        request.request_id = rid
        token = bind_request_id(rid)
        try:
            response = self.get_response(request)
        finally:
            reset_request_id(token)
        response[RESPONSE_HEADER] = rid
        return response
