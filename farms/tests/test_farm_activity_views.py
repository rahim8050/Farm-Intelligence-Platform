"""Tests for farms.activity_views CRUD endpoints."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from django.contrib.auth import get_user_model
from django.core.cache import caches
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from activities.models import Activity
from farms.activity_views import (
    FarmActivityListCreateView,
)
from farms.models import Farm, FarmIntegrationAccess
from integrations.authentication import IntegrationTokenUser
from integrations.tokens import mint_integration_access_token

User = get_user_model()


class FarmActivityIntegrationScopesTests(APITestCase):
    """Unit tests for _integration_scopes helper."""

    def test_scopes_with_dict(self) -> None:
        view = FarmActivityListCreateView()
        request = MagicMock()
        request.auth = {"scope": "read write"}
        result = view._integration_scopes(request)
        assert result == {"read", "write"}

    def test_scopes_with_comma(self) -> None:
        view = FarmActivityListCreateView()
        request = MagicMock()
        request.auth = {"scope": "read,write"}
        result = view._integration_scopes(request)
        assert result == {"read", "write"}

    def test_scopes_empty_dict(self) -> None:
        view = FarmActivityListCreateView()
        request = MagicMock()
        request.auth = {}
        result = view._integration_scopes(request)
        assert result == set()

    def test_scopes_none_auth(self) -> None:
        view = FarmActivityListCreateView()
        request = MagicMock()
        request.auth = None
        result = view._integration_scopes(request)
        assert result == set()

    def test_scopes_object_with_get(self) -> None:
        class MockAuth:
            def get(self, key: str) -> str | None:
                if key == "scope":
                    return "read admin"
                return None

        view = FarmActivityListCreateView()
        request = MagicMock()
        request.auth = MockAuth()
        result = view._integration_scopes(request)
        assert result == {"read", "admin"}

    def test_scopes_object_get_raises(self) -> None:
        class MockAuth:
            def get(self, key: str) -> str:
                raise RuntimeError("boom")

            def __getitem__(self, key: str) -> str:
                if key == "scope":
                    return "read"
                raise KeyError(key)

        view = FarmActivityListCreateView()
        request = MagicMock()
        request.auth = MockAuth()
        result = view._integration_scopes(request)
        assert result == {"read"}

    def test_scopes_subscript_fallback(self) -> None:
        class MockAuth:
            def __getitem__(self, key: str) -> str:
                if key == "scope":
                    return "write"
                raise KeyError(key)

        view = FarmActivityListCreateView()
        request = MagicMock()
        request.auth = MockAuth()
        result = view._integration_scopes(request)
        assert result == {"write"}

    def test_scopes_subscript_raises(self) -> None:
        class MockAuth:
            def __getitem__(self, key: str) -> str:
                raise RuntimeError("boom")

        view = FarmActivityListCreateView()
        request = MagicMock()
        request.auth = MockAuth()
        result = view._integration_scopes(request)
        assert result == set()


class FarmActivityEnforceScopeTests(APITestCase):
    """Unit tests for _enforce_integration_scope helper."""

    def test_non_integration_user_returns(self) -> None:
        view = FarmActivityListCreateView()
        request = MagicMock()
        request.user = MagicMock()
        request.user.__class__ = User
        view._enforce_integration_scope(request, write=False)

    def test_integration_user_missing_scope_raises(self) -> None:
        view = FarmActivityListCreateView()
        request = MagicMock()
        token_user = MagicMock(spec=IntegrationTokenUser)
        request.user = token_user
        request.auth = {}
        from rest_framework.exceptions import PermissionDenied

        with self.assertRaises(PermissionDenied) as ctx:
            view._enforce_integration_scope(request, write=False)
        self.assertIn("missing", str(ctx.exception).lower())

    def test_integration_user_scope_not_permitted_raises(self) -> None:
        view = FarmActivityListCreateView()
        request = MagicMock()
        token_user = MagicMock(spec=IntegrationTokenUser)
        request.user = token_user
        request.auth = {"scope": "read"}
        from rest_framework.exceptions import PermissionDenied

        with self.assertRaises(PermissionDenied) as ctx:
            view._enforce_integration_scope(request, write=True)
        self.assertIn("not permitted", str(ctx.exception).lower())

    def test_integration_user_read_scope_allowed(self) -> None:
        view = FarmActivityListCreateView()
        request = MagicMock()
        token_user = MagicMock(spec=IntegrationTokenUser)
        request.user = token_user
        request.auth = {"scope": "read"}
        view._enforce_integration_scope(request, write=False)

    def test_integration_user_write_scope_allowed(self) -> None:
        view = FarmActivityListCreateView()
        request = MagicMock()
        token_user = MagicMock(spec=IntegrationTokenUser)
        request.user = token_user
        request.auth = {"scope": "write"}
        view._enforce_integration_scope(request, write=True)

    def test_integration_user_admin_scope_allowed(self) -> None:
        view = FarmActivityListCreateView()
        request = MagicMock()
        token_user = MagicMock(spec=IntegrationTokenUser)
        request.user = token_user
        request.auth = {"scope": "admin"}
        view._enforce_integration_scope(request, write=True)


class FarmActivityAPITests(APITestCase):
    """Integration tests for farm activity CRUD endpoints."""

    def setUp(self) -> None:
        caches["default"].clear()
        password = secrets.token_urlsafe(12)
        self.owner = User.objects.create_user(
            username="activity-owner",
            password=password,
        )
        self.other_user = User.objects.create_user(
            username="activity-other",
            password=password,
        )
        self.farm = Farm.objects.create(
            owner=self.owner,
            name="Test Farm",
            slug="test-farm",
            is_active=True,
        )
        self.inactive_farm = Farm.objects.create(
            owner=self.owner,
            name="Inactive Farm",
            slug="inactive-farm",
            is_active=False,
        )
        self.now = datetime.now(UTC)
        self.client.force_authenticate(user=self.owner)

    def _activity_payload(self) -> dict[str, object]:
        return {
            "type": "irrigation",
            "status": "created",
            "scheduled_at": (self.now + timedelta(days=1)).isoformat(),
            "next_due_at": (self.now + timedelta(days=1)).isoformat(),
            "recurrence_type": "none",
            "metadata": {},
        }

    def _integration_client(self, client_id: str, scope: str) -> APIClient:
        access, _ = mint_integration_access_token(
            user_id=client_id, scope=scope
        )
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        return client

    # --- LIST ---

    def test_list_activities_empty(self) -> None:
        resp = self.client.get(
            f"/api/v1/farms/{self.farm.id}/activities/",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        self.assertEqual(body["status"], 0)
        self.assertEqual(body["data"], [])

    def test_list_activities_returns_owned(self) -> None:
        Activity.objects.create(
            owner=self.owner,
            farm=self.farm,
            type="irrigation",
            status="created",
            scheduled_at=self.now,
            next_due_at=self.now,
        )
        resp = self.client.get(
            f"/api/v1/farms/{self.farm.id}/activities/",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        self.assertEqual(len(body["data"]), 1)

    def test_list_activities_filters_by_status(self) -> None:
        Activity.objects.create(
            owner=self.owner,
            farm=self.farm,
            type="irrigation",
            status="created",
            scheduled_at=self.now,
            next_due_at=self.now,
        )
        Activity.objects.create(
            owner=self.owner,
            farm=self.farm,
            type="fertilizer",
            status="success",
            scheduled_at=self.now,
            next_due_at=self.now,
        )
        resp = self.client.get(
            f"/api/v1/farms/{self.farm.id}/activities/?status=created",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        self.assertEqual(len(body["data"]), 1)
        self.assertEqual(body["data"][0]["status"], "created")

    def test_list_activities_filters_by_type(self) -> None:
        Activity.objects.create(
            owner=self.owner,
            farm=self.farm,
            type="irrigation",
            status="created",
            scheduled_at=self.now,
            next_due_at=self.now,
        )
        Activity.objects.create(
            owner=self.owner,
            farm=self.farm,
            type="fertilizer",
            status="created",
            scheduled_at=self.now,
            next_due_at=self.now,
        )
        resp = self.client.get(
            f"/api/v1/farms/{self.farm.id}/activities/?type=irrigation",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        self.assertEqual(len(body["data"]), 1)
        self.assertEqual(body["data"][0]["type"], "irrigation")

    def test_list_activities_respects_limit(self) -> None:
        for i in range(5):
            Activity.objects.create(
                owner=self.owner,
                farm=self.farm,
                type="irrigation",
                status="created",
                scheduled_at=self.now + timedelta(hours=i),
                next_due_at=self.now + timedelta(hours=i),
            )
        resp = self.client.get(
            f"/api/v1/farms/{self.farm.id}/activities/?limit=2",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        self.assertEqual(len(body["data"]), 2)

    def test_list_activities_respects_offset(self) -> None:
        for i in range(5):
            Activity.objects.create(
                owner=self.owner,
                farm=self.farm,
                type="irrigation",
                status="created",
                scheduled_at=self.now + timedelta(hours=i),
                next_due_at=self.now + timedelta(hours=i),
            )
        resp = self.client.get(
            f"/api/v1/farms/{self.farm.id}/activities/?limit=2&offset=3",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        self.assertEqual(len(body["data"]), 2)

    def test_list_activities_denied_for_other_user(self) -> None:
        self.client.force_authenticate(user=self.other_user)
        resp = self.client.get(
            f"/api/v1/farms/{self.farm.id}/activities/",
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_list_activities_denied_for_inactive_farm(self) -> None:
        resp = self.client.get(
            f"/api/v1/farms/{self.inactive_farm.id}/activities/",
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    # --- CREATE ---

    def test_create_activity_success(self) -> None:
        resp = self.client.post(
            f"/api/v1/farms/{self.farm.id}/activities/",
            self._activity_payload(),
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        body = resp.json()
        self.assertEqual(body["status"], 0)
        self.assertEqual(body["data"]["type"], "irrigation")
        self.assertEqual(Activity.objects.count(), 1)
        created = Activity.objects.first()
        assert created is not None
        self.assertEqual(created.farm_id, self.farm.id)

    def test_create_activity_denied_for_other_user(self) -> None:
        self.client.force_authenticate(user=self.other_user)
        resp = self.client.post(
            f"/api/v1/farms/{self.farm.id}/activities/",
            self._activity_payload(),
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_create_activity_invalid_payload(self) -> None:
        resp = self.client.post(
            f"/api/v1/farms/{self.farm.id}/activities/",
            {"type": "invalid", "scheduled_at": "not-a-date"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    # --- RETRIEVE ---

    def test_retrieve_activity_success(self) -> None:
        activity = Activity.objects.create(
            owner=self.owner,
            farm=self.farm,
            type="irrigation",
            status="created",
            scheduled_at=self.now,
            next_due_at=self.now,
        )
        resp = self.client.get(
            f"/api/v1/farms/{self.farm.id}/activities/{activity.id}/",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        self.assertEqual(body["data"]["type"], "irrigation")

    def test_retrieve_activity_not_found(self) -> None:
        resp = self.client.get(
            f"/api/v1/farms/{self.farm.id}/activities/99999/",
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_retrieve_activity_denied_for_other_user(self) -> None:
        activity = Activity.objects.create(
            owner=self.owner,
            farm=self.farm,
            type="irrigation",
            status="created",
            scheduled_at=self.now,
            next_due_at=self.now,
        )
        self.client.force_authenticate(user=self.other_user)
        resp = self.client.get(
            f"/api/v1/farms/{self.farm.id}/activities/{activity.id}/",
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    # --- UPDATE ---

    def test_update_activity_success(self) -> None:
        activity = Activity.objects.create(
            owner=self.owner,
            farm=self.farm,
            type="irrigation",
            status="created",
            scheduled_at=self.now,
            next_due_at=self.now,
        )
        resp = self.client.patch(
            f"/api/v1/farms/{self.farm.id}/activities/{activity.id}/",
            {"type": "irrigation", "metadata": {"updated": True}},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        self.assertEqual(body["data"]["metadata"], {"updated": True})
        activity.refresh_from_db()
        self.assertEqual(activity.metadata, {"updated": True})

    def test_update_activity_denied_for_other_user(self) -> None:
        activity = Activity.objects.create(
            owner=self.owner,
            farm=self.farm,
            type="irrigation",
            status="created",
            scheduled_at=self.now,
            next_due_at=self.now,
        )
        self.client.force_authenticate(user=self.other_user)
        resp = self.client.patch(
            f"/api/v1/farms/{self.farm.id}/activities/{activity.id}/",
            {"type": "irrigation", "metadata": {"hacked": True}},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    # --- DELETE ---

    def test_delete_activity_success(self) -> None:
        activity = Activity.objects.create(
            owner=self.owner,
            farm=self.farm,
            type="irrigation",
            status="created",
            scheduled_at=self.now,
            next_due_at=self.now,
        )
        resp = self.client.delete(
            f"/api/v1/farms/{self.farm.id}/activities/{activity.id}/",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        self.assertEqual(body["data"], None)
        self.assertEqual(Activity.objects.count(), 0)

    def test_delete_activity_denied_for_other_user(self) -> None:
        activity = Activity.objects.create(
            owner=self.owner,
            farm=self.farm,
            type="irrigation",
            status="created",
            scheduled_at=self.now,
            next_due_at=self.now,
        )
        self.client.force_authenticate(user=self.other_user)
        resp = self.client.delete(
            f"/api/v1/farms/{self.farm.id}/activities/{activity.id}/",
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


class FarmActivityIntegrationTokenTests(APITestCase):
    """Tests for integration token access to activity endpoints."""

    def setUp(self) -> None:
        caches["default"].clear()
        password = secrets.token_urlsafe(12)
        self.service_user = User.objects.create_user(
            username="integration-service",
            password=password,
        )
        self.farm = Farm.objects.create(
            owner=self.service_user,
            name="Integration Farm",
            slug="integration-farm",
            is_active=True,
        )
        self.integration_access = FarmIntegrationAccess.objects.create(
            farm=self.farm,
            client_id="test-client",
            is_active=True,
        )
        self.now = datetime.now(UTC)

    def _activity_payload(self) -> dict[str, object]:
        return {
            "type": "irrigation",
            "status": "created",
            "scheduled_at": (self.now + timedelta(days=1)).isoformat(),
            "next_due_at": (self.now + timedelta(days=1)).isoformat(),
            "recurrence_type": "none",
            "metadata": {},
        }

    def test_integration_read_can_list(self) -> None:
        client = self._integration_client("test-client", "read")
        resp = client.get(
            f"/api/v1/farms/{self.farm.id}/activities/",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_integration_write_can_create(self) -> None:
        client = self._integration_client("test-client", "write")
        resp = client.post(
            f"/api/v1/farms/{self.farm.id}/activities/",
            self._activity_payload(),
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_integration_read_cannot_create(self) -> None:
        client = self._integration_client("test-client", "read")
        resp = client.post(
            f"/api/v1/farms/{self.farm.id}/activities/",
            self._activity_payload(),
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_integration_write_can_update(self) -> None:
        activity = Activity.objects.create(
            owner=self.service_user,
            farm=self.farm,
            type="irrigation",
            status="created",
            scheduled_at=self.now,
            next_due_at=self.now,
        )
        client = self._integration_client("test-client", "write")
        resp = client.patch(
            f"/api/v1/farms/{self.farm.id}/activities/{activity.id}/",
            {"type": "irrigation", "metadata": {"integration_updated": True}},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_integration_write_can_delete(self) -> None:
        activity = Activity.objects.create(
            owner=self.service_user,
            farm=self.farm,
            type="irrigation",
            status="created",
            scheduled_at=self.now,
            next_due_at=self.now,
        )
        client = self._integration_client("test-client", "write")
        resp = client.delete(
            f"/api/v1/farms/{self.farm.id}/activities/{activity.id}/",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_integration_admin_can_do_all(self) -> None:
        client = self._integration_client("test-client", "admin")
        resp = client.post(
            f"/api/v1/farms/{self.farm.id}/activities/",
            self._activity_payload(),
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        activity_id = resp.json()["data"]["id"]
        resp = client.get(
            f"/api/v1/farms/{self.farm.id}/activities/{activity_id}/",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        resp = client.patch(
            f"/api/v1/farms/{self.farm.id}/activities/{activity_id}/",
            {"type": "irrigation", "metadata": {"admin_updated": True}},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        resp = client.delete(
            f"/api/v1/farms/{self.farm.id}/activities/{activity_id}/",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_integration_unlisted_client_denied(self) -> None:
        client = self._integration_client("unknown-client", "write")
        resp = client.get(
            f"/api/v1/farms/{self.farm.id}/activities/",
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def _integration_client(self, client_id: str, scope: str) -> APIClient:
        access, _ = mint_integration_access_token(
            user_id=client_id, scope=scope
        )
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        return client
