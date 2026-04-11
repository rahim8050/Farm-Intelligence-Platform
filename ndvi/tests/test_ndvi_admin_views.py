"""Tests for NDVI admin views (circuit breaker reset)."""

from __future__ import annotations

import secrets

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from ndvi.circuit_breaker import CircuitBreaker, register_circuit_breaker


class CircuitBreakerResetTests(APITestCase):
    """Verify the circuit breaker reset admin endpoint."""

    def setUp(self) -> None:
        password = secrets.token_urlsafe(12)
        self.admin = get_user_model().objects.create_superuser(
            username="admin-user",
            email="admin@example.com",
            password=password,
        )
        self.regular_user = get_user_model().objects.create_user(
            username="regular-user",
            email="user@example.com",
            password=password,
        )
        self.url = reverse("ndvi-circuit-breaker-reset")

    def test_requires_admin(self) -> None:
        """Non-admin users should get 403."""
        self.client.force_authenticate(self.regular_user)
        resp = self.client.post(self.url, {"engine": "stac"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_invalid_engine_returns_400(self) -> None:
        """Invalid engine name should return 400."""
        self.client.force_authenticate(self.admin)
        resp = self.client.post(self.url, {"engine": "invalid"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        data = resp.json()
        self.assertIsNone(data["data"])
        self.assertIn("Invalid engine", data["message"])

    def test_success_resets_circuit(self) -> None:
        """Valid reset should return 200 with previous/new state."""
        cb = CircuitBreaker(
            engine="stac",
            failure_threshold=3,
            reset_timeout_secs=1.0,
        )
        # Trip the circuit
        for _ in range(3):
            cb.record_failure()
        self.assertEqual(cb.state, "open")

        register_circuit_breaker(cb)

        try:
            self.client.force_authenticate(self.admin)
            resp = self.client.post(
                self.url, {"engine": "stac"}, format="json"
            )
            self.assertEqual(resp.status_code, status.HTTP_200_OK)
            data = resp.json()
            self.assertEqual(data["status"], 0)
            self.assertEqual(data["data"]["engine"], "stac")
            self.assertEqual(data["data"]["previous_state"], "open")
            self.assertEqual(data["data"]["new_state"], "closed")
            self.assertIn("reset to CLOSED", data["message"])

            # Verify circuit is actually reset
            self.assertEqual(cb.state, "closed")
        finally:
            from ndvi.circuit_breaker import _ENGINE_REGISTRY

            _ENGINE_REGISTRY.pop("stac", None)

    def test_noop_when_already_closed(self) -> None:
        """Resetting an already-closed circuit should succeed."""
        cb = CircuitBreaker(
            engine="sentinelhub",
            failure_threshold=3,
            reset_timeout_secs=1.0,
        )
        register_circuit_breaker(cb)

        try:
            self.client.force_authenticate(self.admin)
            resp = self.client.post(
                self.url, {"engine": "sentinelhub"}, format="json"
            )
            self.assertEqual(resp.status_code, status.HTTP_200_OK)
            data = resp.json()
            self.assertEqual(data["data"]["previous_state"], "closed")
            self.assertEqual(data["data"]["new_state"], "closed")
        finally:
            from ndvi.circuit_breaker import _ENGINE_REGISTRY

            _ENGINE_REGISTRY.pop("sentinelhub", None)
