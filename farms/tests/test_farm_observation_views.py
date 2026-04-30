from __future__ import annotations

from unittest.mock import MagicMock

from farms.observation_views import FarmObservationListCreateView


def test_integration_scopes_with_dict() -> None:
    view = FarmObservationListCreateView()
    request = MagicMock()
    request.auth = {"scope": "read write"}
    result = view._integration_scopes(request)
    assert result == {"read", "write"}


def test_integration_scopes_with_comma() -> None:
    view = FarmObservationListCreateView()
    request = MagicMock()
    request.auth = {"scope": "read,write"}
    result = view._integration_scopes(request)
    assert result == {"read", "write"}


def test_integration_scopes_empty() -> None:
    view = FarmObservationListCreateView()
    request = MagicMock()
    request.auth = {}
    result = view._integration_scopes(request)
    assert result == set()


def test_integration_scopes_none() -> None:
    view = FarmObservationListCreateView()
    request = MagicMock()
    request.auth = None
    result = view._integration_scopes(request)
    assert result == set()


def test_integration_scopes_object_with_get() -> None:
    class MockAuth:
        def get(self, key: str) -> str | None:
            if key == "scope":
                return "read admin"
            return None

    view = FarmObservationListCreateView()
    request = MagicMock()
    request.auth = MockAuth()
    result = view._integration_scopes(request)
    assert result == {"read", "admin"}
