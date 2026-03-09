"""Tests for Nextcloud HMAC authentication."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

import pytest
from django.test import override_settings
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from integrations.hmac import (
    NextcloudHMACVerificationError,
    _get_required_headers,
    _log_hmac_debug,
    body_sha256_hex,
    build_canonical_string,
    compute_hmac_signature_hex,
    verify_nextcloud_hmac_request,
)

factory = APIRequestFactory()


def make_signed_request(
    method: str = "GET",
    path: str = "/api/v1/integrations/nextcloud/ping/",
    client_id: str = "test-client",
    secret_b64: str = "dGVzdC1zZWNyZXQ=",  # noqa: S107 test secret
    body: bytes = b"",
    timestamp: int | None = None,
    nonce: str | None = None,
) -> Request:
    """Create a request with valid HMAC signature."""
    if timestamp is None:
        timestamp = int(time.time())
    if nonce is None:
        nonce = "test-nonce"

    # For GET requests, body should be empty bytes
    if method.upper() == "GET":
        body = b""

    body_sha256 = hashlib.sha256(body).hexdigest()
    canonical_query = ""

    canonical = "\n".join(
        [
            method.upper(),
            path,
            canonical_query,
            str(timestamp),
            nonce,
            body_sha256,
        ]
    )
    secret = base64.b64decode(secret_b64)
    signature = hmac.new(
        secret,
        canonical.encode(),
        hashlib.sha256,
    ).hexdigest()

    if method.upper() == "GET":
        request = factory.get(
            path,
            HTTP_X_CLIENT_ID=client_id,
            HTTP_X_NC_TIMESTAMP=str(timestamp),
            HTTP_X_NC_NONCE=nonce,
            HTTP_X_NC_SIGNATURE=signature,
        )
    else:
        request = factory.post(
            path,
            data=body,
            content_type="application/json",
            HTTP_X_CLIENT_ID=client_id,
            HTTP_X_NC_TIMESTAMP=str(timestamp),
            HTTP_X_NC_NONCE=nonce,
            HTTP_X_NC_SIGNATURE=signature,
        )
    return Request(request)


class TestGetRequiredHeaders:
    def test_missing_client_id_raises(self) -> None:
        request = factory.get("/test/")
        drf_request = Request(request)
        with pytest.raises(NextcloudHMACVerificationError, match="Missing"):
            _get_required_headers(drf_request)

    def test_missing_timestamp_raises(self) -> None:
        request = factory.get("/test/", HTTP_X_CLIENT_ID="test")
        drf_request = Request(request)
        with pytest.raises(NextcloudHMACVerificationError, match="Missing"):
            _get_required_headers(drf_request)

    def test_missing_nonce_raises(self) -> None:
        request = factory.get(
            "/test/",
            HTTP_X_CLIENT_ID="test",
            HTTP_X_NC_TIMESTAMP="123456",
        )
        drf_request = Request(request)
        with pytest.raises(NextcloudHMACVerificationError, match="Missing"):
            _get_required_headers(drf_request)

    def test_missing_signature_raises(self) -> None:
        request = factory.get(
            "/test/",
            HTTP_X_CLIENT_ID="test",
            HTTP_X_NC_TIMESTAMP="123456",
            HTTP_X_NC_NONCE="nonce",
        )
        drf_request = Request(request)
        with pytest.raises(NextcloudHMACVerificationError, match="Missing"):
            _get_required_headers(drf_request)

    def test_invalid_timestamp_raises(self) -> None:
        request = factory.get(
            "/test/",
            HTTP_X_CLIENT_ID="test",
            HTTP_X_NC_TIMESTAMP="not-a-number",
            HTTP_X_NC_NONCE="nonce",
            HTTP_X_NC_SIGNATURE="sig",
        )
        drf_request = Request(request)
        with pytest.raises(NextcloudHMACVerificationError, match="Invalid"):
            _get_required_headers(drf_request)

    def test_alternative_headers_accepted(self) -> None:
        request = factory.get(
            "/test/",
            HTTP_X_NC_CLIENT_ID="test",
            HTTP_X_NC_TIMESTAMP="123456",
            HTTP_X_NC_NONCE="nonce",
            HTTP_X_NC_SIGNATURE="sig",
        )
        drf_request = Request(request)
        headers = _get_required_headers(drf_request)
        assert headers.client_id == "test"
        assert headers.timestamp == 123456
        assert headers.nonce == "nonce"
        assert headers.signature == "sig"


class TestBuildCanonicalString:
    def test_builds_correct_canonical_string(self) -> None:
        result = build_canonical_string(
            method="POST",
            path="/api/test/",
            query_string="",
            timestamp=123456,
            nonce="test-nonce",
            body_sha256=hashlib.sha256(b'{"key": "value"}').hexdigest(),
        )
        body_sha = hashlib.sha256(b'{"key": "value"}').hexdigest()
        expected = f"POST\n/api/test/\n\n123456\ntest-nonce\n{body_sha}"
        assert result == expected

    def test_query_params_sorted(self) -> None:
        result = build_canonical_string(
            method="GET",
            path="/api/test/",
            query_string="z=1&a=2",
            timestamp=123456,
            nonce="nonce",
            body_sha256=hashlib.sha256(b"").hexdigest(),
        )
        assert "a=2&z=1" in result


class TestBodySha256Hex:
    def test_returns_hex_for_post_body(self) -> None:
        body = b'{"key": "value"}'
        result = body_sha256_hex(method="POST", body=body)
        expected = hashlib.sha256(body).hexdigest()
        assert result == expected

    def test_ignores_body_for_get(self) -> None:
        result = body_sha256_hex(method="GET", body=b'{"ignored": true}')
        expected = hashlib.sha256(b"").hexdigest()
        assert result == expected


class TestComputeHmacSignatureHex:
    def test_returns_valid_signature(self) -> None:
        secret = b"test-secret"
        canonical = "GET\n/test/\n\n123456\nnonce\nabc123"
        result = compute_hmac_signature_hex(
            secret=secret, canonical_string=canonical
        )
        expected = hmac.new(
            secret, canonical.encode(), hashlib.sha256
        ).hexdigest()
        assert result == expected


class TestVerifyTimestampViaRequest:
    @override_settings(
        NEXTCLOUD_HMAC_MAX_SKEW_SECONDS=300,
        INTEGRATION_HMAC_CLIENTS_JSON='{"test-client": "dGVzdC1zZWNyZXQ="}',
        NEXTCLOUD_HMAC_ENABLED=True,
    )
    def test_old_timestamp_raises(self) -> None:
        old_timestamp = int(time.time()) - 400
        request = make_signed_request(timestamp=old_timestamp)
        with pytest.raises(NextcloudHMACVerificationError, match="timestamp"):
            verify_nextcloud_hmac_request(request)

    @override_settings(
        NEXTCLOUD_HMAC_MAX_SKEW_SECONDS=300,
        INTEGRATION_HMAC_CLIENTS_JSON='{"test-client": "dGVzdC1zZWNyZXQ="}',
        NEXTCLOUD_HMAC_ENABLED=True,
    )
    def test_future_timestamp_raises(self) -> None:
        future_timestamp = int(time.time()) + 400
        request = make_signed_request(timestamp=future_timestamp)
        with pytest.raises(NextcloudHMACVerificationError, match="timestamp"):
            verify_nextcloud_hmac_request(request)

    @override_settings(
        NEXTCLOUD_HMAC_MAX_SKEW_SECONDS=300,
        INTEGRATION_HMAC_CLIENTS_JSON='{"test-client": "dGVzdC1zZWNyZXQ="}',
        NEXTCLOUD_HMAC_ENABLED=True,
    )
    def test_valid_timestamp_passes(self) -> None:
        valid_timestamp = int(time.time())
        request = make_signed_request(timestamp=valid_timestamp)
        client_id = verify_nextcloud_hmac_request(request)
        assert client_id == "test-client"


class TestVerifyNonceViaRequest:
    @override_settings(
        NEXTCLOUD_HMAC_NONCE_TTL_SECONDS=360,
        INTEGRATION_HMAC_CLIENTS_JSON='{"test-client": "dGVzdC1zZWNyZXQ="}',
        NEXTCLOUD_HMAC_ENABLED=True,
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache"
            }
        },
    )
    def test_replay_nonce_raises(self) -> None:
        nonce = "replay-nonce"
        # First use should succeed
        request = make_signed_request(nonce=nonce)
        verify_nextcloud_hmac_request(request)
        # Second use should fail
        request2 = make_signed_request(nonce=nonce)
        with pytest.raises(NextcloudHMACVerificationError, match="replay"):
            verify_nextcloud_hmac_request(request2)

    @override_settings(
        NEXTCLOUD_HMAC_NONCE_TTL_SECONDS=360,
        INTEGRATION_HMAC_CLIENTS_JSON='{"test-client": "dGVzdC1zZWNyZXQ="}',
        NEXTCLOUD_HMAC_ENABLED=True,
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache"
            }
        },
    )
    def test_unique_nonce_passes(self) -> None:
        request1 = make_signed_request(nonce="unique-nonce-1")
        verify_nextcloud_hmac_request(request1)
        request2 = make_signed_request(nonce="unique-nonce-2")
        verify_nextcloud_hmac_request(request2)  # Should not raise


class TestGetClientSecret:
    pass  # Tested via verify_nextcloud_hmac_request


class TestLogHmacDebug:
    @override_settings(NEXTCLOUD_HMAC_DEBUG_LOGGING=False)
    def test_noop_when_debug_disabled(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        secret = b"test-secret"
        _log_hmac_debug(
            client_id="test",
            method="GET",
            path="/test/",
            body_sha256="abc123",
            canonical="canonical",
            signature="sig",
            expected_signature="expected",
            secret=secret,
        )
        # When debug is disabled, the function returns early
        pass

    @override_settings(NEXTCLOUD_HMAC_DEBUG_LOGGING=True)
    def test_logs_when_debug_enabled(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        logger = logging.getLogger("integrations.hmac")
        logger.setLevel(logging.DEBUG)

        secret = b"test-secret"
        _log_hmac_debug(
            client_id="test",
            method="GET",
            path="/test/",
            body_sha256="abc123",
            canonical="canonical",
            signature="sig",
            expected_signature="expected",
            secret=secret,
        )
        assert caplog.record_tuples


class TestVerifyNextcloudHmacRequest:
    @override_settings(
        NEXTCLOUD_HMAC_ENABLED=True,
        INTEGRATION_HMAC_CLIENTS_JSON='{"test-client": "dGVzdC1zZWNyZXQ="}',
        NEXTCLOUD_HMAC_MAX_SKEW_SECONDS=300,
        NEXTCLOUD_HMAC_NONCE_TTL_SECONDS=360,
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache"
            }
        },
    )
    def test_valid_signature_passes(self) -> None:
        request = make_signed_request()
        client_id = verify_nextcloud_hmac_request(request)
        assert client_id == "test-client"

    @override_settings(NEXTCLOUD_HMAC_ENABLED=False)
    def test_disabled_returns_client_id(self) -> None:
        request = factory.get(
            "/test/",
            HTTP_X_CLIENT_ID="test-client",
        )
        drf_request = Request(request)
        client_id = verify_nextcloud_hmac_request(drf_request)
        assert client_id == "test-client"

    @override_settings(
        NEXTCLOUD_HMAC_ENABLED=True,
        INTEGRATION_HMAC_CLIENTS_JSON='{"test-client": "dGVzdC1zZWNyZXQ="}',
    )
    def test_invalid_signature_raises(self) -> None:
        request = factory.get(
            "/test/",
            HTTP_X_CLIENT_ID="test-client",
            HTTP_X_NC_TIMESTAMP=str(int(time.time())),
            HTTP_X_NC_NONCE="nonce",
            HTTP_X_NC_SIGNATURE="invalid-signature",
        )
        drf_request = Request(request)
        with pytest.raises(NextcloudHMACVerificationError, match="signature"):
            verify_nextcloud_hmac_request(drf_request)

    @override_settings(
        NEXTCLOUD_HMAC_ENABLED=True,
        INTEGRATION_HMAC_CLIENTS_JSON='{"test-client": "dGVzdC1zZWNyZXQ="}',
    )
    def test_wrong_client_id_raises(self) -> None:
        request = make_signed_request(client_id="wrong-client")
        with pytest.raises(
            NextcloudHMACVerificationError, match="Unknown Nextcloud client"
        ):
            verify_nextcloud_hmac_request(request)
