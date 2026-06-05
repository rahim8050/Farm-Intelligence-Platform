"""Tests for the request-id / correlation-id middleware and helpers.

Per ``prompts/p4-staff-engineer-review.md`` #10, every API response
carries an ``X-Request-ID`` header and a ``request_id`` field inside
the envelope. The same id is exposed to views, picked up by Celery
tasks via a ``before_task_publish`` signal, and restored to its
previous value on ``task_postrun``.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from typing import Any
from unittest.mock import MagicMock

from django.test import (
    Client,
    RequestFactory,
    SimpleTestCase,
    override_settings,
)
from rest_framework.response import Response

from config.api.request_id import (
    HEADER_NAME,
    RESPONSE_HEADER,
    RequestIdMiddleware,
    bind_request_id,
    current_request_id,
    new_request_id,
    reset_request_id,
)
from config.api.responses import error_response, success_response


class CurrentRequestIdTests(SimpleTestCase):
    """The contextvar helpers behave like a thread-/task-local slot."""

    def test_default_is_empty(self) -> None:
        self.assertEqual(current_request_id(), "")

    def test_bind_and_reset_round_trip(self) -> None:
        token = bind_request_id("abc123")
        try:
            self.assertEqual(current_request_id(), "abc123")
        finally:
            reset_request_id(token)
        self.assertEqual(current_request_id(), "")

    def test_nested_bind_restores_outer(self) -> None:
        outer = bind_request_id("outer")
        try:
            inner = bind_request_id("inner")
            try:
                self.assertEqual(current_request_id(), "inner")
            finally:
                reset_request_id(inner)
            self.assertEqual(current_request_id(), "outer")
        finally:
            reset_request_id(outer)
        self.assertEqual(current_request_id(), "")

    def test_new_request_id_is_uuid_hex(self) -> None:
        rid = new_request_id()
        self.assertEqual(len(rid), 32)
        int(rid, 16)  # raises if not hex


class RequestIdMiddlewareTests(SimpleTestCase):
    """The middleware reads, generates, and stamps the response header."""

    def setUp(self) -> None:
        self.factory = RequestFactory()
        self.observed: dict[str, str] = {}

        def view(request: Any) -> Response:
            self.observed["rid"] = request.request_id
            self.observed["current"] = current_request_id()
            return Response({"ok": True})

        self.middleware = RequestIdMiddleware(view)

    def _request(self, incoming: str | None) -> object:
        if incoming is None:
            return self.factory.get("/api/v1/test/")
        return self.factory.get("/api/v1/test/", HTTP_X_REQUEST_ID=incoming)

    def test_generates_id_when_header_missing(self) -> None:
        response = self.middleware(self._request(None))
        self.assertIn(RESPONSE_HEADER, response)
        rid = response[RESPONSE_HEADER]
        self.assertEqual(rid, self.observed["rid"])
        self.assertEqual(len(rid), 32)
        self.assertEqual(current_request_id(), "")

    def test_honours_incoming_header(self) -> None:
        response = self.middleware(self._request("client-abc-123"))
        self.assertEqual(response[RESPONSE_HEADER], "client-abc-123")
        self.assertEqual(self.observed["rid"], "client-abc-123")
        self.assertEqual(self.observed["current"], "client-abc-123")
        self.assertEqual(current_request_id(), "")

    def test_replaces_blank_header_with_new_id(self) -> None:
        response = self.middleware(self._request("   "))
        rid = response[RESPONSE_HEADER]
        self.assertNotEqual(rid.strip(), "")
        self.assertEqual(len(rid), 32)

    def test_reads_underscore_meta_key(self) -> None:
        # Django only adds the meta key when the header is set on the request.
        request = self.factory.get("/x/", HTTP_X_REQUEST_ID="abc")
        self.assertEqual(request.META[HEADER_NAME], "abc")


class EnvelopeRequestIdTests(SimpleTestCase):
    """``success_response`` and ``error_response`` embed the active id."""

    def setUp(self) -> None:
        # Bind a known id, remember the token for teardown.
        self._token = bind_request_id("rid-xyz")
        # Track nested tokens so we can clean up if the test rebinds.
        self._extra_tokens: list = []

    def tearDown(self) -> None:
        for tok in reversed(self._extra_tokens):
            try:
                reset_request_id(tok)
            except ValueError:
                pass
        try:
            reset_request_id(self._token)
        except ValueError:
            pass

    def test_success_envelope_has_request_id(self) -> None:
        response = success_response({"a": 1})
        self.assertEqual(response.data["request_id"], "rid-xyz")

    def test_error_envelope_has_request_id(self) -> None:
        response = error_response("nope")
        self.assertEqual(response.data["request_id"], "rid-xyz")

    def test_envelope_request_id_is_null_when_unset(self) -> None:
        token = bind_request_id("")
        self._extra_tokens.append(token)
        response = success_response({"a": 1})
        self.assertIsNone(response.data["request_id"])


@override_settings(ROOT_URLCONF="config.urls")
class EnvelopeRequestIdHttpTests(SimpleTestCase):
    """End-to-end check that the response carries the header + field."""

    def test_response_carries_request_id(self) -> None:
        client = Client(HTTP_X_REQUEST_ID="trace-abc")
        response = client.get("/api/schema/")
        self.assertEqual(response[RESPONSE_HEADER], "trace-abc")
        self.assertEqual(response.status_code, 200)


class CelerySignalTests(SimpleTestCase):
    """``before_task_publish`` copies the active id into headers."""

    def setUp(self) -> None:
        from config import celery as celery_mod

        self._propagate = celery_mod._propagate_request_id

    def test_propagate_writes_header(self) -> None:
        token = bind_request_id("rid-from-test")
        try:
            headers: dict[str, object] = OrderedDict()
            self._propagate(sender="x", headers=headers)
            self.assertEqual(headers.get("request_id"), "rid-from-test")
        finally:
            reset_request_id(token)

    def test_propagate_uses_env_when_slot_empty(self) -> None:
        token = bind_request_id("")
        try:
            os.environ["NDVI_ACTIVE_REQUEST_ID"] = "rid-from-env"
            try:
                headers: dict[str, object] = OrderedDict()
                self._propagate(sender="x", headers=headers)
                self.assertEqual(headers.get("request_id"), "rid-from-env")
            finally:
                del os.environ["NDVI_ACTIVE_REQUEST_ID"]
        finally:
            reset_request_id(token)

    def test_propagate_noop_when_no_id_available(self) -> None:
        token = bind_request_id("")
        os.environ.pop("NDVI_ACTIVE_REQUEST_ID", None)
        try:
            headers: dict[str, object] = OrderedDict()
            self._propagate(sender="x", headers=headers)
            self.assertNotIn("request_id", headers)
        finally:
            reset_request_id(token)


class CeleryPrerunPostrunTests(SimpleTestCase):
    """``task_prerun`` binds; ``task_postrun`` restores."""

    def setUp(self) -> None:
        from config import celery as celery_mod

        self._bind = celery_mod._bind_request_id
        self._restore = celery_mod._restore_request_id
        # Start clean: bind an empty id, hold the token for cleanup.
        self._token = bind_request_id("")

    def tearDown(self) -> None:
        try:
            reset_request_id(self._token)
        except ValueError:
            pass

    def test_bind_and_restore_round_trip(self) -> None:
        task = MagicMock()
        task.request.get.return_value = "rid-from-headers"
        self._bind(task_id="t-1", task=task)
        self.assertEqual(current_request_id(), "rid-from-headers")
        self._restore(task_id="t-1")
        self.assertEqual(current_request_id(), "")

    def test_bind_generates_when_header_missing(self) -> None:
        task = MagicMock()
        task.request.get.return_value = ""
        self._bind(task_id="t-2", task=task)
        rid = current_request_id()
        self.assertEqual(len(rid), 32)
        self._restore(task_id="t-2")
        self.assertEqual(current_request_id(), "")
