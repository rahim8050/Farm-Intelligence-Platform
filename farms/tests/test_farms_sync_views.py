from __future__ import annotations

from unittest.mock import MagicMock

from farms.sync_views import _integration_scopes


def test_integration_scopes_with_dict() -> None:
    request = MagicMock()
    request.auth = {"scope": "read write"}
    result = _integration_scopes(request)
    assert result == {"read", "write"}


def test_integration_scopes_with_comma() -> None:
    request = MagicMock()
    request.auth = {"scope": "read,write"}
    result = _integration_scopes(request)
    assert result == {"read", "write"}


def test_integration_scopes_empty() -> None:
    request = MagicMock()
    request.auth = {}
    result = _integration_scopes(request)
    assert result == set()


def test_integration_scopes_none() -> None:
    request = MagicMock()
    request.auth = None
    result = _integration_scopes(request)
    assert result == set()
