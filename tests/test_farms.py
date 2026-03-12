from __future__ import annotations

import secrets

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from farms.models import Farm


class FarmCreationValidationTests(APITestCase):
    """Verify the farm API surfaces validation errors for Nextcloud."""

    def setUp(self) -> None:
        password = secrets.token_urlsafe(12)
        self.user = get_user_model().objects.create_user(
            username="farm-owner",
            password=password,
        )
        self.client.force_authenticate(self.user)

    def test_requires_name(self) -> None:
        """POST /api/v1/farms/ fails without the required name field."""
        response = self.client.post(
            reverse("farm-list"), data={}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIsInstance(response.data, dict)
        self.assertIn("name", response.data)
        self.assertIn(
            "required",
            " ".join(map(str, response.data["name"])).lower(),
        )

    def test_duplicate_name_returns_conflict(self) -> None:
        """Reusing the same name for one owner raises a validation error."""
        Farm.objects.create(owner=self.user, name="Existing Farm")
        response = self.client.post(
            reverse("farm-list"),
            data={"name": "Existing Farm"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data.get("name"), ["Farm name already exists."]
        )
