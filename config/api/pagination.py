"""Pagination helpers that fit the project's response envelope.

Per ``prompts/p3followup.md`` (API and Data Lifecycle Standards),
user-owned collection endpoints must be paginated by default using
DRF pagination classes. The standards call for ``page`` and
``page_size`` query parameters and a hard upper bound on
``page_size``.

This module provides:

- :class:`EnvelopedPageNumberPagination` - a small
  ``PageNumberPagination`` subclass with sane defaults.
- :func:`paginated_response` - the one-shot helper that paginates a
  queryset, serialises the page, and wraps the result in the
  project's ``success_response`` envelope (with the DRF
  ``count``/``next``/``previous``/``results`` shape living inside
  ``data``).
- :func:`paginated_envelope_serializer` - the matching OpenAPI
  schema helper, mirroring the runtime shape so Swagger and the
  real response agree.
"""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import OpenApiParameter, inline_serializer
from rest_framework import serializers
from rest_framework.pagination import PageNumberPagination
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import Serializer

from config.api.openapi import success_envelope_serializer
from config.api.responses import success_response

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


class EnvelopedPageNumberPagination(PageNumberPagination):
    """``PageNumberPagination`` tuned for our envelope.

    - ``page`` query param drives the offset.
    - ``page_size`` query param overrides the default page size.
    - The response payload (built by ``get_paginated_response``) keeps
      DRF's default ``{count, next, previous, results}`` shape, which
      :func:`paginated_response` then wraps in our envelope.
    """

    page_size = DEFAULT_PAGE_SIZE
    page_size_query_param = "page_size"
    max_page_size = MAX_PAGE_SIZE
    page_query_param = "page"

    def get_paginated_response(  # type: ignore[override]
        self, serialized_data: list[Any]
    ) -> dict[str, Any]:
        """Return a dict matching DRF's default page shape.

        This mirrors ``PageNumberPagination.get_paginated_response``
        but returns a plain dict (not a ``Response``) so the helper
        can wrap it in the project's envelope. The signature is
        narrowed intentionally; the parent's
        ``get_paginated_response`` returns a ``Response`` but we
        never use it (the helper below calls this method directly).
        """
        page = self.page
        if page is None:
            return {
                "count": 0,
                "next": None,
                "previous": None,
                "results": serialized_data,
            }
        return {
            "count": page.paginator.count,
            "next": self.get_next_link(),
            "previous": self.get_previous_link(),
            "results": serialized_data,
        }


def paginated_response(
    queryset: Any,
    serializer_class: type[Serializer],
    request: Request,
    *,
    message: str = "OK",
    page_size: int | None = None,
    max_page_size: int | None = None,
    context: dict[str, Any] | None = None,
) -> Response:
    """Paginate ``queryset`` and return a :class:`Response` in our envelope.

    Args:
        queryset: The Django queryset to paginate.
        serializer_class: The DRF serializer class to use for each
            item. The helper calls ``.data`` on each instance.
        request: The current :class:`rest_framework.request.Request`
            (needed by the pagination class to read query params and
            build absolute next/previous links).
        message: Envelope ``message`` string.
        page_size: Optional override for the default page size.
        max_page_size: Optional override for the maximum page size.
        context: Optional extra ``context`` dict for the serializer.

    Returns:
        A 200 :class:`Response` whose ``data`` dict is

        .. code-block:: json

            {
                "count": 42,
                "next": "http://.../favorites/?page=3",
                "previous": null,
                "results": [ ... ]
            }
    """
    paginator = EnvelopedPageNumberPagination()
    if page_size is not None:
        paginator.page_size = page_size
    if max_page_size is not None:
        paginator.max_page_size = max_page_size

    page = paginator.paginate_queryset(queryset, request, view=None)
    ctx = {"request": request, **(context or {})}
    serialized: list[Any]
    if page is not None:
        serialized = list(serializer_class(page, many=True, context=ctx).data)
    else:
        serialized = []
    payload = paginator.get_paginated_response(serialized)
    return success_response(payload, message=message)


def paginated_envelope_serializer(
    name: str,
    *,
    item: serializers.Field,
) -> Serializer:
    """Build an OpenAPI schema for a paginated envelope.

    Mirrors the runtime shape produced by :func:`paginated_response`:
    an envelope whose ``data`` is a dict with ``count``,
    ``next``/``previous`` URLs, and a ``results`` array of ``item``.
    """
    data = inline_serializer(
        name=f"{name}Data",
        fields={
            "count": serializers.IntegerField(),
            "next": serializers.CharField(allow_null=True),
            "previous": serializers.CharField(allow_null=True),
            "results": item,
        },
    )
    return success_envelope_serializer(name, data=data)


def pagination_parameters() -> list[OpenApiParameter]:
    """OpenAPI parameters for ``page`` and ``page_size`` query params.

    Use as ``parameters=pagination_parameters()`` inside
    ``@extend_schema`` on any list endpoint that goes through
    :func:`paginated_response`. The defaults match
    :class:`EnvelopedPageNumberPagination`.
    """
    return [
        OpenApiParameter(
            name="page",
            type=int,
            location=OpenApiParameter.QUERY,
            required=False,
            description="1-based page number. Defaults to 1.",
        ),
        OpenApiParameter(
            name="page_size",
            type=int,
            location=OpenApiParameter.QUERY,
            required=False,
            description=(
                f"Number of items per page. Defaults to "
                f"{DEFAULT_PAGE_SIZE}, max {MAX_PAGE_SIZE}."
            ),
        ),
    ]
