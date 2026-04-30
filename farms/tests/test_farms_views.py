from __future__ import annotations

from unittest.mock import MagicMock

from farms.views import _auth_type


def test_auth_type_api_key() -> None:
    request = MagicMock()
    request.META = {"HTTP_X_API_KEY": "test-key"}
    result = _auth_type(request)
    assert result == "api_key"


def test_auth_type_jwt_bearer() -> None:
    request = MagicMock()
    request.META = {"HTTP_AUTHORIZATION": "Bearer token123"}
    result = _auth_type(request)
    assert result == "jwt_bearer"


def test_auth_type_jwt_bearer_lowercase() -> None:
    request = MagicMock()
    request.META = {"HTTP_AUTHORIZATION": "bearer token123"}
    result = _auth_type(request)
    assert result == "jwt_bearer"


def test_auth_type_authorization() -> None:
    request = MagicMock()
    request.META = {"HTTP_AUTHORIZATION": "Basic abc123"}
    result = _auth_type(request)
    assert result == "authorization"


def test_auth_type_unknown() -> None:
    request = MagicMock()
    request.META = {}
    result = _auth_type(request)
    assert result == "unknown"
