from __future__ import annotations

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from integrations.tokens import mint_integration_access_token


def _auth_client(*, access_token: str) -> APIClient:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")
    return client


@pytest.mark.django_db
def test_nextcloud_status_requires_auth() -> None:
    client = APIClient()

    resp = client.get("/api/v1/integrations/nextcloud/status/")

    assert resp.status_code == status.HTTP_401_UNAUTHORIZED
    assert resp["Content-Type"].startswith("application/json")
    assert resp.json()["status"] == 1


@pytest.mark.django_db
def test_nextcloud_status_returns_payload() -> None:
    access, _ = mint_integration_access_token(user_id="client-1", scope="read")
    client = _auth_client(access_token=access)

    resp = client.get("/api/v1/integrations/nextcloud/status/")

    assert resp.status_code == status.HTTP_200_OK
    body = resp.json()
    assert body["status"] == 0
    data = body["data"]
    assert data["ok"] is True
    assert data["capabilities"]["png_preview"] is True
    assert isinstance(data.get("version"), str)
    assert data.get("server_time")


@pytest.mark.django_db
def test_nextcloud_preview_requires_auth_returns_json_error() -> None:
    client = APIClient()

    resp = client.get(
        "/api/v1/integrations/nextcloud/preview.png",
        HTTP_ACCEPT="image/png",
    )

    assert resp.status_code == status.HTTP_401_UNAUTHORIZED
    assert resp["Content-Type"].startswith("application/json")
    assert resp.json()["status"] == 1


@pytest.mark.django_db
def test_nextcloud_preview_returns_png() -> None:
    access, _ = mint_integration_access_token(user_id="client-1", scope="read")
    client = _auth_client(access_token=access)

    resp = client.get("/api/v1/integrations/nextcloud/preview.png")

    assert resp.status_code == status.HTTP_200_OK
    assert resp["Content-Type"].startswith("image/png")
    assert resp["Cache-Control"] == "no-store"
    assert resp.content.startswith(b"\x89PNG\r\n\x1a\n")
