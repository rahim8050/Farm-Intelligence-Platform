from __future__ import annotations

from rest_framework_simplejwt.exceptions import InvalidToken

from farms.authentication import _is_missing_scope


def test_is_missing_scope_true() -> None:
    exc = InvalidToken("scope missing")
    assert _is_missing_scope(exc) is True


def test_is_missing_scope_false() -> None:
    exc = InvalidToken("token expired")
    assert _is_missing_scope(exc) is False


def test_is_missing_scope_no_args() -> None:
    exc = InvalidToken()
    assert _is_missing_scope(exc) is False
