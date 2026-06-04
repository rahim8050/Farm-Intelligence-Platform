"""Shared pytest fixtures for the ``alerts`` test suite.

The user / farm builders live here so the test files do not have to
duplicate the boilerplate. We use ``secrets.token_urlsafe`` to avoid
collisions in the in-memory SQLite DB.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from typing import Any

import pytest
from django.contrib.auth import get_user_model

from farms.models import Farm


@pytest.fixture
def make_user() -> Callable[..., Any]:
    """Return a callable that builds a fresh user."""

    def _make(username: str | None = None) -> Any:
        user_model = get_user_model()
        return user_model.objects.create_user(
            username=username or f"u-{secrets.token_urlsafe(8)}",
            password=secrets.token_urlsafe(16),
        )

    return _make


@pytest.fixture
def make_farm() -> Callable[..., Farm]:
    """Return a callable that builds a fresh farm for the given owner."""

    def _make(owner: Any, name: str = "Test Farm") -> Farm:
        return Farm.objects.create(
            owner=owner,
            name=name,
            centroid_lat=0.0,
            centroid_lon=0.0,
        )

    return _make
