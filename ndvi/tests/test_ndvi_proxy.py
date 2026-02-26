from __future__ import annotations

import secrets
from unittest.mock import MagicMock, patch

import httpx
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase


class NdviProxyTests(APITestCase):
    def setUp(self) -> None:
        password = secrets.token_urlsafe(16)
        self.user = get_user_model().objects.create_user(
            username="owner",
            password=password,
            email="owner@example.com",
        )
        self.url = "/api/v1/ndvi"

    @override_settings(
        NDVI_PROXY_ENABLED=True,
        NDVI_SERVICE_URL="http://ndvi-service:8081",
        PROXY_TIMEOUT_SECONDS=5.0,
    )
    @patch("config.api.proxy.httpx.request")
    def test_ingest_proxy_success(self, mock_request: MagicMock) -> None:
        """Valid payloads are forwarded to the NDVI service."""

        upstream = httpx.Response(
            status_code=status.HTTP_201_CREATED,
            json={
                "status": 0,
                "message": "Created",
                "data": {"status": "created"},
                "errors": None,
            },
            request=httpx.Request(
                "POST", "http://ndvi-service:8081/api/v1/ndvi"
            ),
        )
        mock_request.return_value = upstream
        self.client.force_authenticate(user=self.user)
        payload = {
            "farm_id": "00000000-0000-0000-0000-000000000001",
            "timestamp": "2025-01-01T00:00:00Z",
            "mean": 0.5,
            "min": 0.4,
            "max": 0.6,
            "source": "test",
            "geometry": None,
        }
        resp = self.client.post(self.url, payload, format="json")

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        body = resp.json()
        self.assertEqual(body.get("status"), 0)
        mock_request.assert_called_once()

    @override_settings(
        NDVI_PROXY_ENABLED=True,
        NDVI_SERVICE_URL="http://ndvi-service:8081",
    )
    @patch("config.api.proxy.httpx.request")
    def test_ingest_validation_error(self, mock_request: MagicMock) -> None:
        """Invalid payloads are rejected before proxying."""

        self.client.force_authenticate(user=self.user)
        payload = {
            "farm_id": "00000000-0000-0000-0000-000000000001",
            "timestamp": "2025-01-01T00:00:00Z",
            "mean": 1.2,
            "min": 0.4,
            "max": 0.6,
        }
        resp = self.client.post(self.url, payload, format="json")

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        body = resp.json()
        self.assertEqual(body.get("status"), 1)
        mock_request.assert_not_called()
