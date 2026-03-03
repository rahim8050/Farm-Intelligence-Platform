from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any, cast
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password as django_check_password
from django.contrib.auth.models import AnonymousUser
from django.core.cache import caches
from django.core.exceptions import ImproperlyConfigured
from django.test.utils import override_settings
from django.utils import timezone
from django.utils.module_loading import import_string
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.settings import api_settings
from rest_framework.test import APIRequestFactory, APITestCase
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication

from api_keys.auth import (
    _client_ip,
    _eligible_keys,
    generate_plaintext_key,
    hash_api_key,
    validate_api_key,
    validate_api_key_with_reason,
)
from api_keys.authentication import ApiKeyAuthentication
from api_keys.models import ApiKey, ApiKeyScope
from api_keys.openapi import ApiKeyAuthenticationScheme
from api_keys.permissions import ApiKeyScopePermission, HasValidApiKey
from api_keys.serializers import ApiKeyCreateSerializer
from api_keys.throttling import ApiKeyRateThrottle
from api_keys.views import _client_ip as view_client_ip

_RF: dict[str, Any] = cast(dict[str, Any], settings.REST_FRAMEWORK)
_RF_RATES: dict[str, str] = cast(
    dict[str, str], _RF.get("DEFAULT_THROTTLE_RATES", {})
)


class ApiKeyTests(APITestCase):
    register_url = "/api/v1/auth/register/"
    keys_url = "/api/v1/keys/"

    def setUp(self) -> None:
        super().setUp()
        caches["default"].clear()
        caches["throttle"].clear()

    def _register_and_login(self, username: str = "zoe") -> tuple[str, str]:
        resp = self.client.post(
            self.register_url,
            {
                "username": username,
                "email": f"{username}@example.com",
                "password": "StrongPass123!",
                "password2": "StrongPass123!",
            },
            format="json",
        )
        data = resp.json()["data"]
        return data["tokens"]["access"], data["tokens"]["refresh"]

    def _create_api_key(
        self,
        access: str,
        name: str = "My Key",
        *,
        scope: str | None = None,
    ) -> tuple[str, ApiKey]:
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        payload: dict[str, Any] = {"name": name, "expires_at": None}
        if scope is not None:
            payload["scope"] = scope
        resp = self.client.post(
            self.keys_url,
            payload,
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        data = resp.json()["data"]
        plaintext = data["api_key"]
        api_key = ApiKey.objects.get(id=data["id"])
        return plaintext, api_key

    def test_create_api_key_and_store_only_hash(self) -> None:
        access, _ = self._register_and_login("apiuser")
        plaintext, api_key = self._create_api_key(access, name="My Key")
        self.assertNotEqual(api_key.key_hash, plaintext)
        self.assertTrue(api_key.key_hash.startswith("pbkdf2_"))
        self.assertFalse(hasattr(api_key, "api_key"))
        self.assertEqual(api_key.scope, ApiKeyScope.READ)

    def test_list_api_keys_never_exposes_plaintext(self) -> None:
        access, _ = self._register_and_login("apilist")
        plaintext, api_key = self._create_api_key(access, name="List Key")
        list_resp = self.client.get(self.keys_url)
        self.assertEqual(list_resp.status_code, status.HTTP_200_OK)
        body = list_resp.json()
        self.assertEqual(body["status"], 0)
        for item in body["data"]:
            self.assertNotIn("api_key", item)
            self.assertNotIn("key_hash", item)
        self.assertNotEqual(api_key.key_hash, plaintext)

    def test_create_api_key_persists_requested_scope(self) -> None:
        access, _ = self._register_and_login("scopecreate")
        _, api_key = self._create_api_key(access, scope=ApiKeyScope.WRITE)
        self.assertEqual(api_key.scope, ApiKeyScope.WRITE)

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        list_resp = self.client.get(self.keys_url)
        self.assertEqual(list_resp.status_code, status.HTTP_200_OK)
        body = list_resp.json()
        self.assertEqual(body["status"], 0)
        scopes = {item["scope"] for item in body["data"]}
        self.assertIn(ApiKeyScope.WRITE, scopes)

    def test_revoke_api_key_and_validate_helper(self) -> None:
        access, _ = self._register_and_login("apirevoke")
        plaintext, api_key = self._create_api_key(access, name="Revoke Me")
        revoke_resp = self.client.delete(f"{self.keys_url}{api_key.id}/")
        self.assertEqual(revoke_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(revoke_resp.json()["status"], 0)

        api_key.refresh_from_db()
        self.assertIsNotNone(api_key.revoked_at)
        self.assertIsNone(validate_api_key(plaintext))

    def test_cannot_revoke_other_users_key(self) -> None:
        access1, _ = self._register_and_login("owner")
        _, api_key = self._create_api_key(access1, name="Owner Key")

        access2, _ = self._register_and_login("intruder")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access2}")
        resp = self.client.delete(f"{self.keys_url}{api_key.id}/")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.json()["status"], 1)

    def test_api_key_header_authenticates_requests(self) -> None:
        access, _ = self._register_and_login("headerauth")
        plaintext, api_key = self._create_api_key(access, name="Header Key")

        class ProtectedView(APIView):
            authentication_classes = (ApiKeyAuthentication,)
            permission_classes = (AllowAny,)

            def get(self, request: Request) -> Response:  # type: ignore[override]
                return Response({"ok": True})

        factory = APIRequestFactory()
        resp = ProtectedView.as_view()(
            factory.get("/protected", HTTP_X_API_KEY=plaintext)
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["ok"], True)
        api_key.refresh_from_db()
        self.assertIsNotNone(api_key.last_used_at)

    def test_missing_or_invalid_api_key_denied(self) -> None:
        resp = self.client.get(self.keys_url)
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

        resp_invalid = self.client.get(
            self.keys_url,
            HTTP_X_API_KEY="wk_live_notreal",
        )
        self.assertEqual(
            resp_invalid.status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_revoked_or_expired_key_denied(self) -> None:
        access, _ = self._register_and_login("expired")
        plaintext, api_key = self._create_api_key(access, name="Short Lived")

        revoke_resp = self.client.delete(f"{self.keys_url}{api_key.id}/")
        self.assertEqual(revoke_resp.status_code, status.HTTP_200_OK)

        self.client.credentials()
        resp = self.client.get(self.keys_url, HTTP_X_API_KEY=plaintext)
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

        api_key.refresh_from_db()
        api_key.revoked_at = None
        api_key.expires_at = timezone.now() - timedelta(minutes=1)
        api_key.save(update_fields=["revoked_at", "expires_at"])

        expired_resp = self.client.get(
            self.keys_url,
            HTTP_X_API_KEY=plaintext,
        )
        self.assertEqual(
            expired_resp.status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_api_key_str_and_pepper_property(self) -> None:
        password = secrets.token_urlsafe(12)
        user = get_user_model().objects.create_user(
            username="pepper",
            email="pepper@example.com",
            password=password,
        )
        api_key = ApiKey.objects.create(
            user=user,
            name="Service Key",
            key_hash="hash",
            prefix="wk_live",
            last4="1234",
            scope=ApiKeyScope.READ,
        )
        self.assertEqual(str(api_key), "Service Key (wk_live…1234)")
        self.assertEqual(api_key.pepper, settings.DJANGO_API_KEY_PEPPER)

    def test_api_key_create_serializer_rejects_past_expiry(self) -> None:
        serializer = ApiKeyCreateSerializer(
            data={
                "name": "Expired Key",
                "expires_at": timezone.now() - timedelta(days=1),
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("expires_at", serializer.errors)

    def test_api_key_auth_cannot_manage_keys(self) -> None:
        access, _ = self._register_and_login("onlyjwt")
        plaintext, api_key = self._create_api_key(access, name="My Key")

        self.client.credentials()
        list_resp = self.client.get(self.keys_url, HTTP_X_API_KEY=plaintext)
        self.assertEqual(list_resp.status_code, status.HTTP_401_UNAUTHORIZED)

        post_resp = self.client.post(
            self.keys_url,
            {"name": "Blocked"},
            format="json",
            HTTP_X_API_KEY=plaintext,
        )
        self.assertEqual(post_resp.status_code, status.HTTP_401_UNAUTHORIZED)

        delete_resp = self.client.delete(
            f"{self.keys_url}{api_key.id}/",
            HTTP_X_API_KEY=plaintext,
        )
        self.assertEqual(delete_resp.status_code, status.HTTP_401_UNAUTHORIZED)

        rotate_resp = self.client.post(
            f"{self.keys_url}{api_key.id}/rotate/",
            HTTP_X_API_KEY=plaintext,
        )
        self.assertEqual(rotate_resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_rotate_api_key_returns_new_and_revokes_old(self) -> None:
        access, _ = self._register_and_login("rotate")
        old_plaintext, old_key = self._create_api_key(access, name="Old Key")

        rotate_resp = self.client.post(
            f"{self.keys_url}{old_key.id}/rotate/",
            {"name": "Rotated Key"},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {access}",
        )
        self.assertEqual(rotate_resp.status_code, status.HTTP_201_CREATED)
        body = rotate_resp.json()
        self.assertEqual(body["status"], 0)
        data = cast(dict[str, Any], body["data"])
        new_plaintext = data["api_key"]
        new_id = data["id"]

        old_key.refresh_from_db()
        self.assertIsNotNone(old_key.revoked_at)
        self.assertIsNone(validate_api_key(old_plaintext))
        self.assertIsNotNone(validate_api_key(new_plaintext))
        self.assertNotEqual(str(old_key.id), new_id)

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        list_resp = self.client.get(self.keys_url)
        self.assertEqual(list_resp.status_code, status.HTTP_200_OK)
        for item in list_resp.json()["data"]:
            self.assertNotIn("api_key", item)

        self.client.credentials()
        old_auth_resp = self.client.get(
            self.keys_url,
            HTTP_X_API_KEY=old_plaintext,
        )
        self.assertEqual(
            old_auth_resp.status_code, status.HTTP_401_UNAUTHORIZED
        )

    def test_revoke_already_revoked_key(self) -> None:
        access, _ = self._register_and_login("revoke-twice")
        _, api_key = self._create_api_key(access, name="Revoke Twice")

        first = self.client.delete(f"{self.keys_url}{api_key.id}/")
        self.assertEqual(first.status_code, status.HTTP_200_OK)

        second = self.client.delete(f"{self.keys_url}{api_key.id}/")
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertEqual(second.json()["status"], 0)

    def test_rotate_from_revoked_key(self) -> None:
        access, _ = self._register_and_login("rotated-revoked")
        _, api_key = self._create_api_key(access, name="Revoked Key")
        api_key.revoked_at = timezone.now()
        api_key.save(update_fields=["revoked_at"])

        rotate_resp = self.client.post(
            f"{self.keys_url}{api_key.id}/rotate/",
            {"name": "Rehydrated"},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {access}",
        )
        self.assertEqual(rotate_resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(rotate_resp.json()["status"], 0)

    def test_api_key_scope_permission_blocks_unsafe_methods(self) -> None:
        access, _ = self._register_and_login("scopeperm")
        plaintext, _ = self._create_api_key(access, scope=ApiKeyScope.READ)

        class ScopedView(APIView):
            authentication_classes = (ApiKeyAuthentication,)
            permission_classes = (AllowAny, ApiKeyScopePermission)

            def get(self, request: Request) -> Response:  # type: ignore[override]
                return Response({"ok": True})

            def post(self, request: Request) -> Response:  # type: ignore[override]
                return Response({"ok": True})

        factory = APIRequestFactory()
        view = ScopedView.as_view()
        ok_resp = view(factory.get("/t", HTTP_X_API_KEY=plaintext))
        self.assertEqual(ok_resp.status_code, status.HTTP_200_OK)

        blocked_resp = view(factory.post("/t", HTTP_X_API_KEY=plaintext))
        self.assertEqual(blocked_resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_api_key_scope_permission_does_not_block_non_api_key_auth(
        self,
    ) -> None:
        permission = ApiKeyScopePermission()
        factory = APIRequestFactory()
        drf_request = Request(factory.post("/t"))
        drf_request.auth = "jwt"
        self.assertTrue(permission.has_permission(drf_request, object()))

    def test_api_key_scope_permission_admin_allows_write(self) -> None:
        user = get_user_model().objects.create_user(
            username="scope-admin",
            email="scope-admin@example.com",
            password=secrets.token_urlsafe(12),
        )
        api_key = ApiKey.objects.create(
            user=user,
            name="Admin Key",
            key_hash=hash_api_key(generate_plaintext_key()),
            prefix="wk_live_admin",
            last4="9999",
            scope=ApiKeyScope.ADMIN,
        )
        permission = ApiKeyScopePermission()
        factory = APIRequestFactory()
        drf_request = Request(factory.post("/t"))
        drf_request.auth = api_key
        self.assertTrue(permission.has_permission(drf_request, object()))

    def test_has_valid_api_key_permission(self) -> None:
        user = get_user_model().objects.create_user(
            username="perm-user",
            email="perm-user@example.com",
            password=secrets.token_urlsafe(12),
        )
        plaintext = generate_plaintext_key()
        ApiKey.objects.create(
            user=user,
            name="Perm Key",
            key_hash=hash_api_key(plaintext),
            prefix=plaintext[:12],
            last4=plaintext[-4:],
            scope=ApiKeyScope.READ,
        )
        permission = HasValidApiKey()
        factory = APIRequestFactory()
        drf_request = Request(factory.get("/t", HTTP_X_API_KEY=plaintext))
        self.assertTrue(permission.has_permission(drf_request, object()))

        missing = Request(factory.get("/t"))
        self.assertFalse(permission.has_permission(missing, object()))

    def test_last_used_at_write_is_throttled(self) -> None:
        access, _ = self._register_and_login("lastused")
        plaintext, api_key = self._create_api_key(access, name="Used Key")
        factory = APIRequestFactory()

        class ProtectedView(APIView):
            authentication_classes = (ApiKeyAuthentication,)
            permission_classes = (AllowAny,)

            def get(self, request: Request) -> Response:  # type: ignore[override]
                return Response({"ok": True})

        view = ProtectedView.as_view()

        t0 = timezone.now()
        with patch("api_keys.auth.timezone.now", return_value=t0):
            first = view(factory.get("/p", HTTP_X_API_KEY=plaintext))
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        api_key.refresh_from_db()
        self.assertEqual(api_key.last_used_at, t0)

        t1 = t0 + timedelta(minutes=2)
        with patch("api_keys.auth.timezone.now", return_value=t1):
            second = view(factory.get("/p", HTTP_X_API_KEY=plaintext))
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        api_key.refresh_from_db()
        self.assertEqual(api_key.last_used_at, t0)

        t2 = t0 + timedelta(minutes=6)
        with patch("api_keys.auth.timezone.now", return_value=t2):
            third = view(factory.get("/p", HTTP_X_API_KEY=plaintext))
        self.assertEqual(third.status_code, status.HTTP_200_OK)
        api_key.refresh_from_db()
        self.assertEqual(api_key.last_used_at, t2)

    def test_audit_logs_do_not_leak_plaintext(self) -> None:
        access, _ = self._register_and_login("audit")
        with self.assertLogs("api_keys", level="INFO") as logs:
            plaintext, api_key = self._create_api_key(
                access, name="Audited", scope=ApiKeyScope.WRITE
            )
        joined = "\n".join(logs.output)
        self.assertIn("api_key.created", joined)
        self.assertIn(f"user_id={api_key.user_id}", joined)
        self.assertIn(f"key_id={api_key.id}", joined)
        self.assertNotIn(plaintext, joined)

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        with self.assertLogs("api_keys", level="INFO") as revoke_logs:
            revoke_resp = self.client.delete(f"{self.keys_url}{api_key.id}/")
        self.assertEqual(revoke_resp.status_code, status.HTTP_200_OK)
        revoke_joined = "\n".join(revoke_logs.output)
        self.assertIn("api_key.revoked", revoke_joined)
        self.assertIn(f"user_id={api_key.user_id}", revoke_joined)
        self.assertIn(f"key_id={api_key.id}", revoke_joined)
        self.assertNotIn(plaintext, revoke_joined)

    def test_audit_logs_rotation_does_not_leak_plaintext(self) -> None:
        access, _ = self._register_and_login("audit-rotate")
        old_plaintext, api_key = self._create_api_key(
            access, name="Old", scope=ApiKeyScope.WRITE
        )

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        with self.assertLogs("api_keys", level="INFO") as logs:
            rotate_resp = self.client.post(
                f"{self.keys_url}{api_key.id}/rotate/",
                {"name": "New"},
                format="json",
            )
        self.assertEqual(rotate_resp.status_code, status.HTTP_201_CREATED)
        new_plaintext = rotate_resp.json()["data"]["api_key"]

        joined = "\n".join(logs.output)
        self.assertIn("api_key.rotated", joined)
        self.assertIn(f"user_id={api_key.user_id}", joined)
        self.assertIn(f"old_key_id={api_key.id}", joined)
        self.assertNotIn(old_plaintext, joined)
        self.assertNotIn(new_plaintext, joined)

    @override_settings(API_KEY_AUTH_LOG_SUCCESS=True)
    def test_audit_logs_api_key_auth_success_and_revoked_failure(self) -> None:
        access, _ = self._register_and_login("audit-auth")
        plaintext, api_key = self._create_api_key(access, name="Auth Log Key")

        class ProtectedView(APIView):
            authentication_classes = (ApiKeyAuthentication,)
            permission_classes = (AllowAny,)

            def get(self, request: Request) -> Response:  # type: ignore[override]
                return Response({"ok": True})

        factory = APIRequestFactory()
        view = ProtectedView.as_view()

        with self.assertLogs("api_keys", level="INFO") as ok_logs:
            ok_resp = view(factory.get("/p", HTTP_X_API_KEY=plaintext))
        self.assertEqual(ok_resp.status_code, status.HTTP_200_OK)
        ok_joined = "\n".join(ok_logs.output)
        self.assertIn("api_key.auth.success", ok_joined)
        self.assertIn(f"user_id={api_key.user_id}", ok_joined)
        self.assertIn(f"key_id={api_key.id}", ok_joined)
        self.assertNotIn(plaintext, ok_joined)

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        self.client.delete(f"{self.keys_url}{api_key.id}/")

        with self.assertLogs("api_keys", level="WARNING") as bad_logs:
            bad_resp = view(factory.get("/p", HTTP_X_API_KEY=plaintext))
        self.assertEqual(bad_resp.status_code, status.HTTP_401_UNAUTHORIZED)
        bad_joined = "\n".join(bad_logs.output)
        self.assertIn("api_key.auth.failure", bad_joined)
        self.assertIn("reason=revoked", bad_joined)
        self.assertIn(f"user_id={api_key.user_id}", bad_joined)
        self.assertIn(f"key_id={api_key.id}", bad_joined)
        self.assertNotIn(plaintext, bad_joined)

    @override_settings(API_KEY_AUTH_LOG_SUCCESS=False)
    def test_api_key_auth_success_logging_can_be_disabled(self) -> None:
        access, _ = self._register_and_login("audit-auth-muted")
        plaintext, _ = self._create_api_key(access, name="Muted Auth Log Key")

        class ProtectedView(APIView):
            authentication_classes = (ApiKeyAuthentication,)
            permission_classes = (AllowAny,)

            def get(self, request: Request) -> Response:  # type: ignore[override]
                return Response({"ok": True})

        factory = APIRequestFactory()
        view = ProtectedView.as_view()

        with self.assertNoLogs("api_keys", level="INFO"):
            ok_resp = view(factory.get("/p", HTTP_X_API_KEY=plaintext))
        self.assertEqual(ok_resp.status_code, status.HTTP_200_OK)

    @override_settings(
        REST_FRAMEWORK={
            **_RF,
            "DEFAULT_THROTTLE_CLASSES": (
                "api_keys.throttling.ApiKeyRateThrottle",
            ),
            "DEFAULT_THROTTLE_RATES": {
                **_RF_RATES,
                "api_key": "2/min",
            },
        }
    )
    def test_per_key_throttling_is_isolated(self) -> None:
        api_settings.reload()
        ApiKeyRateThrottle = import_string(
            "api_keys.throttling.ApiKeyRateThrottle"
        )

        class ThrottledView(APIView):
            authentication_classes = (ApiKeyAuthentication,)
            permission_classes = (AllowAny,)
            throttle_classes = (ApiKeyRateThrottle,)

            def get(self, request: Request) -> Response:  # type: ignore[override]
                return Response({"ok": True})

        access, _ = self._register_and_login("ratelimit")
        key1, _ = self._create_api_key(access, name="Key One")
        key2, _ = self._create_api_key(access, name="Key Two")

        factory = APIRequestFactory()
        view = ThrottledView.as_view()

        for _ in range(2):
            ok_resp = view(factory.get("/t", HTTP_X_API_KEY=key1))
            self.assertEqual(ok_resp.status_code, status.HTTP_200_OK)

        blocked = view(factory.get("/t", HTTP_X_API_KEY=key1))
        self.assertEqual(
            blocked.status_code, status.HTTP_429_TOO_MANY_REQUESTS
        )
        self.assertEqual(blocked.data["status"], 1)
        self.assertEqual(blocked.data["message"], "Too Many Requests")
        self.assertIsNone(blocked.data["data"])
        errors = cast(dict[str, Any], blocked.data["errors"])
        self.assertIn("detail", errors)
        self.assertIsInstance(errors["detail"], str)
        self.assertIn("wait", errors)
        self.assertIsInstance(errors["wait"], (int, float))

        other_key_resp = view(factory.get("/t", HTTP_X_API_KEY=key2))
        self.assertEqual(other_key_resp.status_code, status.HTTP_200_OK)

    def test_throttle_uses_throttle_cache_alias(self) -> None:
        throttle = ApiKeyRateThrottle()
        self.assertIs(throttle.cache, caches["throttle"])

    def test_throttle_ignores_header_without_api_key_auth(self) -> None:
        access, _ = self._register_and_login("jwtthrottle")

        class JwtThrottleView(APIView):
            authentication_classes = (JWTAuthentication,)
            permission_classes = (AllowAny,)
            throttle_classes = (ApiKeyRateThrottle,)

            def get(self, request: Request) -> Response:  # type: ignore[override]
                return Response({"ok": True})

        view = JwtThrottleView.as_view()
        factory = APIRequestFactory()

        for _ in range(3):
            resp = view(
                factory.get(
                    "/t",
                    HTTP_AUTHORIZATION=f"Bearer {access}",
                    HTTP_X_API_KEY="wk_live_fake",
                )
            )
            self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_validate_api_key_with_reason_variants(self) -> None:
        user = get_user_model().objects.create_user(
            username="reason-user",
            email="reason-user@example.com",
            password=secrets.token_urlsafe(12),
        )

        bad_prefix_key = "not_a_key"
        key, reason = validate_api_key_with_reason(bad_prefix_key)
        self.assertIsNone(key)
        self.assertEqual(reason, "invalid")

        no_match = generate_plaintext_key()
        key, reason = validate_api_key_with_reason(no_match)
        self.assertIsNone(key)
        self.assertEqual(reason, "invalid")

        mismatched_plaintext = generate_plaintext_key()
        ApiKey.objects.create(
            user=user,
            name="Mismatch",
            key_hash=hash_api_key(f"{mismatched_plaintext}x"),
            prefix=mismatched_plaintext[:12],
            last4=mismatched_plaintext[-4:],
            scope=ApiKeyScope.READ,
        )
        key, reason = validate_api_key_with_reason(mismatched_plaintext)
        self.assertIsNone(key)
        self.assertEqual(reason, "hash_mismatch")

    def test_validate_api_key_with_reason_expired(self) -> None:
        user = get_user_model().objects.create_user(
            username="expired-user",
            email="expired-user@example.com",
            password=secrets.token_urlsafe(12),
        )
        plaintext = generate_plaintext_key()
        expired_at = timezone.now() - timedelta(minutes=1)
        api_key = ApiKey.objects.create(
            user=user,
            name="Expired",
            key_hash=hash_api_key(plaintext),
            prefix=plaintext[:12],
            last4=plaintext[-4:],
            expires_at=expired_at,
            scope=ApiKeyScope.READ,
        )
        key, reason = validate_api_key_with_reason(plaintext)
        self.assertEqual(key, api_key)
        self.assertEqual(reason, "expired")

    @override_settings(API_KEY_AUTH_CACHE_TTL_SECONDS=60)
    def test_validate_api_key_with_reason_uses_auth_cache_for_hot_key(
        self,
    ) -> None:
        user = get_user_model().objects.create_user(
            username="cache-user",
            email="cache-user@example.com",
            password=secrets.token_urlsafe(12),
        )
        plaintext = generate_plaintext_key()
        api_key = ApiKey.objects.create(
            user=user,
            name="Cached",
            key_hash=hash_api_key(plaintext),
            prefix=plaintext[:12],
            last4=plaintext[-4:],
            scope=ApiKeyScope.READ,
        )

        with patch(
            "api_keys.auth.check_password",
            wraps=django_check_password,
        ) as mocked_check_password:
            first_key, first_reason = validate_api_key_with_reason(plaintext)
            second_key, second_reason = validate_api_key_with_reason(plaintext)

        self.assertEqual(first_reason, "ok")
        self.assertEqual(second_reason, "ok")
        self.assertEqual(first_key, api_key)
        self.assertEqual(second_key, api_key)
        self.assertEqual(mocked_check_password.call_count, 1)

    @override_settings(API_KEY_AUTH_CACHE_TTL_SECONDS=60)
    def test_validate_api_key_with_reason_cached_key_respects_revocation(
        self,
    ) -> None:
        user = get_user_model().objects.create_user(
            username="cache-revoked",
            email="cache-revoked@example.com",
            password=secrets.token_urlsafe(12),
        )
        plaintext = generate_plaintext_key()
        api_key = ApiKey.objects.create(
            user=user,
            name="Revoked Cached",
            key_hash=hash_api_key(plaintext),
            prefix=plaintext[:12],
            last4=plaintext[-4:],
            scope=ApiKeyScope.READ,
        )

        first_key, first_reason = validate_api_key_with_reason(plaintext)
        self.assertEqual(first_reason, "ok")
        self.assertEqual(first_key, api_key)

        api_key.revoked_at = timezone.now()
        api_key.save(update_fields=["revoked_at"])

        second_key, second_reason = validate_api_key_with_reason(plaintext)
        self.assertEqual(second_key, api_key)
        self.assertEqual(second_reason, "revoked")

    def test_authentication_denies_inactive_user(self) -> None:
        user = get_user_model().objects.create_user(
            username="inactive",
            email="inactive@example.com",
            password=secrets.token_urlsafe(12),
            is_active=False,
        )
        plaintext = generate_plaintext_key()
        ApiKey.objects.create(
            user=user,
            name="Inactive Key",
            key_hash=hash_api_key(plaintext),
            prefix=plaintext[:12],
            last4=plaintext[-4:],
            scope=ApiKeyScope.READ,
        )

        class ProtectedView(APIView):
            authentication_classes = (ApiKeyAuthentication,)
            permission_classes = (AllowAny,)

            def get(self, request: Request) -> Response:  # type: ignore[override]
                return Response({"ok": True})

        factory = APIRequestFactory()
        resp = ProtectedView.as_view()(
            factory.get("/p", HTTP_X_API_KEY=plaintext)
        )
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_client_ip_helpers(self) -> None:
        factory = APIRequestFactory()
        drf_request = Request(
            factory.get("/t", HTTP_X_FORWARDED_FOR=" 1.2.3.4, 5.6.7.8")
        )
        self.assertEqual(_client_ip(drf_request), "1.2.3.4")
        self.assertEqual(view_client_ip(drf_request), "1.2.3.4")

    def test_eligible_keys_filters_revoked_and_expired(self) -> None:
        user = get_user_model().objects.create_user(
            username="eligible",
            email="eligible@example.com",
            password=secrets.token_urlsafe(12),
        )
        plaintext = generate_plaintext_key()
        prefix = plaintext[:12]
        last4 = plaintext[-4:]
        ApiKey.objects.create(
            user=user,
            name="Active",
            key_hash=hash_api_key(plaintext),
            prefix=prefix,
            last4=last4,
            scope=ApiKeyScope.READ,
        )
        ApiKey.objects.create(
            user=user,
            name="Revoked",
            key_hash=hash_api_key(plaintext),
            prefix=prefix,
            last4=last4,
            revoked_at=timezone.now(),
            scope=ApiKeyScope.READ,
        )
        ApiKey.objects.create(
            user=user,
            name="Expired",
            key_hash=hash_api_key(plaintext),
            prefix=prefix,
            last4=last4,
            expires_at=timezone.now() - timedelta(days=1),
            scope=ApiKeyScope.READ,
        )
        active = list(_eligible_keys(prefix, last4))
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].name, "Active")

    def test_apikey_view_queryset_anonymous_user(self) -> None:
        view = import_string("api_keys.views.ApiKeyView")()
        factory = APIRequestFactory()
        drf_request = Request(factory.get("/keys/"))
        drf_request.user = AnonymousUser()
        view.request = drf_request
        self.assertEqual(list(view.get_queryset()), [])

    @override_settings(
        REST_FRAMEWORK={
            **_RF,
            "DEFAULT_THROTTLE_RATES": {
                k: v for k, v in _RF_RATES.items() if k != "api_key"
            },
        }
    )
    def test_throttle_rate_missing_returns_none(self) -> None:
        api_settings.reload()
        throttle = ApiKeyRateThrottle()
        self.assertIsNone(throttle.get_rate())

    @override_settings(
        REST_FRAMEWORK={
            **_RF,
            "DEFAULT_THROTTLE_RATES": {**_RF_RATES, "api_key": 5},
        }
    )
    def test_throttle_rate_invalid_type_raises(self) -> None:
        api_settings.reload()
        with self.assertRaises(ImproperlyConfigured):
            ApiKeyRateThrottle()

    def test_openapi_security_definition(self) -> None:
        scheme = ApiKeyAuthenticationScheme(target=ApiKeyAuthentication())
        definition = scheme.get_security_definition(auto_schema=None)
        self.assertEqual(
            definition,
            {"type": "apiKey", "in": "header", "name": "X-API-Key"},
        )
