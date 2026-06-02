from __future__ import annotations

import secrets
from typing import Final, Protocol, cast

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.cache import caches
from django.test import override_settings
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework import serializers, status
from rest_framework.test import APITestCase

from accounts.auth_backends import UsernameOrEmailBackend
from accounts.serializers import PasswordChangeSerializer

type JSONScalar = str | int | float | bool | None
type JSONValue = JSONScalar | list[JSONValue] | dict[str, JSONValue]


class JsonClientResponse(Protocol):
    status_code: int

    def json(self) -> dict[str, JSONValue]: ...


class AccountsTests(APITestCase):
    REGISTER_URL: Final[str] = "/api/v1/auth/register/"
    LOGIN_URL: Final[str] = "/api/v1/auth/login/"
    REFRESH_URL: Final[str] = "/api/v1/auth/token/refresh/"
    ME_URL: Final[str] = "/api/v1/auth/me/"
    PW_CHANGE_URL: Final[str] = "/api/v1/auth/password/change/"
    PW_RESET_URL: Final[str] = "/api/v1/auth/password/reset/"
    PW_RESET_CONFIRM_URL: Final[str] = "/api/v1/auth/password/reset/confirm/"
    PW_RESET_MESSAGE: Final[str] = (
        "If an account exists for this email, a reset link has been sent."
    )
    PW_RESET_CONFIRM_MESSAGE: Final[str] = "Password has been reset."

    def _user(self) -> str:
        return f"user_{secrets.token_hex(6)}"

    def _email(self, username: str) -> str:
        return f"{username}@example.com"

    def _pw(self) -> str:
        # Validator-friendly and non-static.
        lower = "abcdefghijklmnopqrstuvwxyz"
        upper = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        digits = "0123456789"
        special = "!@#$%^&*"
        return (
            secrets.choice(upper)
            + secrets.choice(lower)
            + secrets.choice(digits)
            + secrets.choice(special)
            + secrets.token_urlsafe(16)
        )

    def _as_dict(
        self, value: JSONValue | None, label: str
    ) -> dict[str, JSONValue]:
        if not isinstance(value, dict):
            self.fail(f"Expected dict for {label}")
        return value

    def _as_str(self, value: JSONValue | None, label: str) -> str:
        if not isinstance(value, str):
            self.fail(f"Expected str for {label}")
        return value

    def _register(
        self,
        username: str | None = None,
        email: str | None = None,
        password: str | None = None,
    ) -> JsonClientResponse:
        u = username or self._user()
        e = email or self._email(u)
        pw = password or self._pw()
        payload = {
            "username": u,
            "email": e,
            "password": pw,
            "password2": pw,
        }
        return cast(
            JsonClientResponse,
            self.client.post(self.REGISTER_URL, payload, format="json"),
        )

    def _login(self, identifier: str, password: str) -> JsonClientResponse:
        return cast(
            JsonClientResponse,
            self.client.post(
                self.LOGIN_URL,
                {"identifier": identifier, "password": password},
                format="json",
            ),
        )

    def _password_reset_request(self, email: str) -> JsonClientResponse:
        return cast(
            JsonClientResponse,
            self.client.post(
                self.PW_RESET_URL,
                {"email": email},
                format="json",
            ),
        )

    def _password_reset_confirm(
        self, uid: str, token: str, new_password: str
    ) -> JsonClientResponse:
        payload = {
            "uid": uid,
            "token": token,
            "new_password": new_password,
        }
        return cast(
            JsonClientResponse,
            self.client.post(
                self.PW_RESET_CONFIRM_URL,
                payload,
                format="json",
            ),
        )

    def test_register_success_returns_tokens(self) -> None:
        resp = self._register()
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

        body = resp.json()
        self.assertEqual(body.get("status"), 0)

        data = self._as_dict(body.get("data"), "data")
        self.assertIn("tokens", data)
        self.assertIn("user", data)

    def test_register_password_mismatch_returns_error(self) -> None:
        u = self._user()
        pw1 = self._pw()
        pw2 = self._pw()

        payload = {
            "username": u,
            "email": self._email(u),
            "password": pw1,
            "password2": pw2,
        }
        resp = cast(
            JsonClientResponse,
            self.client.post(self.REGISTER_URL, payload, format="json"),
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

        body = resp.json()
        self.assertEqual(body.get("status"), 1)
        self.assertIsNotNone(body.get("errors"))

    def test_register_duplicate_username_or_email_case_insensitive(
        self,
    ) -> None:
        pw = self._pw()
        base = self._user()

        self._register(username=base, email=self._email(base), password=pw)

        resp = self._register(
            username=base.upper(),
            email=self._email(base).upper(),
            password=pw,
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.json().get("status"), 1)

    def test_login_with_username_identifier(self) -> None:
        pw = self._pw()
        u = self._user()
        self._register(username=u, email=self._email(u), password=pw)

        resp = self._login(u, pw)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        body = resp.json()
        self.assertEqual(body.get("status"), 0)

        data = self._as_dict(body.get("data"), "data")
        tokens = self._as_dict(data.get("tokens"), "data.tokens")
        self.assertIn("access", tokens)

    def test_login_with_email_identifier_case_insensitive(self) -> None:
        pw = self._pw()
        u = self._user()
        e = self._email(u)
        self._register(username=u, email=e, password=pw)

        resp = self._login(e.upper(), pw)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json().get("status"), 0)

    def test_login_wrong_password_generic_error(self) -> None:
        pw_ok = self._pw()
        pw_bad = f"{pw_ok}x"
        u = self._user()

        self._register(username=u, email=self._email(u), password=pw_ok)

        resp = self._login(u, pw_bad)
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(resp.json().get("status"), 1)

    def test_token_refresh_returns_new_access_token(self) -> None:
        resp = self._register()

        body = resp.json()
        data = self._as_dict(body.get("data"), "data")
        tokens = self._as_dict(data.get("tokens"), "data.tokens")
        refresh = self._as_str(tokens.get("refresh"), "data.tokens.refresh")

        refresh_resp = cast(
            JsonClientResponse,
            self.client.post(
                self.REFRESH_URL,
                {"refresh": refresh},
                format="json",
            ),
        )
        self.assertEqual(refresh_resp.status_code, status.HTTP_200_OK)

        out = refresh_resp.json()
        self.assertEqual(out.get("status"), 0)

        out_data = self._as_dict(out.get("data"), "data")
        self.assertIn("access", out_data)

    def test_me_endpoint_requires_jwt(self) -> None:
        u = self._user()
        resp = self._register(username=u, email=self._email(u))

        body = resp.json()
        data = self._as_dict(body.get("data"), "data")
        tokens = self._as_dict(data.get("tokens"), "data.tokens")
        access = self._as_str(tokens.get("access"), "data.tokens.access")

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        me_resp = cast(JsonClientResponse, self.client.get(self.ME_URL))
        self.assertEqual(me_resp.status_code, status.HTTP_200_OK)

        me_body = me_resp.json()
        self.assertEqual(me_body.get("status"), 0)

        me_data = self._as_dict(me_body.get("data"), "data")
        self.assertEqual(me_data.get("username"), u)

    def test_password_change_success_and_failure(self) -> None:
        initial_pw = self._pw()
        u = self._user()
        resp = self._register(
            username=u,
            email=self._email(u),
            password=initial_pw,
        )

        body = resp.json()
        data = self._as_dict(body.get("data"), "data")
        tokens = self._as_dict(data.get("tokens"), "data.tokens")
        access = self._as_str(tokens.get("access"), "data.tokens.access")

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

        bad_resp = cast(
            JsonClientResponse,
            self.client.post(
                self.PW_CHANGE_URL,
                {
                    "old_password": self._pw(),
                    "new_password": self._pw(),
                    "new_password2": self._pw(),
                },
                format="json",
            ),
        )
        self.assertEqual(bad_resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(bad_resp.json().get("status"), 1)

        new_pw = self._pw()
        good_resp = cast(
            JsonClientResponse,
            self.client.post(
                self.PW_CHANGE_URL,
                {
                    "old_password": initial_pw,
                    "new_password": new_pw,
                    "new_password2": new_pw,
                },
                format="json",
            ),
        )
        self.assertEqual(good_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(good_resp.json().get("status"), 0)

        self.client.credentials()
        relogin = self._login(u, new_pw)
        self.assertEqual(relogin.status_code, status.HTTP_200_OK)

    def test_password_change_serializer_rejects_invalid_context(self) -> None:
        serializer = PasswordChangeSerializer(
            data={
                "old_password": "irrelevant",
                "new_password": "NewPass123!",
                "new_password2": "NewPass123!",
            },
            context={"user": "not-a-user"},
        )

        self.assertFalse(serializer.is_valid())
        self.assertEqual(
            serializer.errors["non_field_errors"][0], "Invalid user context."
        )

    def test_password_change_serializer_rejects_mismatched_new_password(
        self,
    ) -> None:
        password = self._pw()
        user = get_user_model().objects.create_user(
            username=self._user(),
            email=self._email("pw-change"),
            password=password,
        )
        serializer = PasswordChangeSerializer(
            data={
                "old_password": password,
                "new_password": "NewPass123!",
                "new_password2": "Mismatch123!",
            },
            context={"user": user},
        )

        self.assertFalse(serializer.is_valid())
        self.assertEqual(
            serializer.errors["new_password2"][0], "Passwords do not match."
        )

    def test_password_change_serializer_save_rejects_invalid_context(
        self,
    ) -> None:
        password = self._pw()
        user = get_user_model().objects.create_user(
            username=self._user(),
            email=self._email("pw-save"),
            password=password,
        )
        serializer = PasswordChangeSerializer(
            data={
                "old_password": password,
                "new_password": "NewPass123!",
                "new_password2": "NewPass123!",
            },
            context={"user": user},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.context["user"] = "invalid"

        with self.assertRaisesMessage(
            serializers.ValidationError, "Invalid user context."
        ):
            serializer.save()

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        FRONTEND_RESET_URL="https://frontend.example/reset",
        DEFAULT_FROM_EMAIL="noreply@example.com",
    )
    def test_password_reset_request_existing_email_sends_email(self) -> None:
        username = self._user()
        email = self._email(username)
        get_user_model().objects.create_user(
            username=username,
            email=email,
            password=self._pw(),
        )

        mail.outbox.clear()
        resp = self._password_reset_request(email)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        body = resp.json()
        self.assertEqual(body.get("status"), 0)
        self.assertEqual(body.get("message"), self.PW_RESET_MESSAGE)
        self.assertIsNone(body.get("data"))
        self.assertIsNone(body.get("errors"))
        self.assertEqual(len(mail.outbox), 1)

    def test_password_reset_request_invalid_email_returns_error(self) -> None:
        resp = self._password_reset_request("not-an-email")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

        body = resp.json()
        self.assertEqual(body.get("status"), 1)
        errors = self._as_dict(body.get("errors"), "errors")
        self.assertIn("email", errors)

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        FRONTEND_RESET_URL="https://frontend.example/reset",
        DEFAULT_FROM_EMAIL="noreply@example.com",
    )
    def test_password_reset_request_non_existing_email_sends_no_email(
        self,
    ) -> None:
        mail.outbox.clear()
        resp = self._password_reset_request("missing@example.com")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        body = resp.json()
        self.assertEqual(body.get("status"), 0)
        self.assertEqual(body.get("message"), self.PW_RESET_MESSAGE)
        self.assertIsNone(body.get("data"))
        self.assertIsNone(body.get("errors"))
        self.assertEqual(len(mail.outbox), 0)

    def test_password_reset_confirm_valid_token_resets_password(self) -> None:
        username = self._user()
        email = self._email(username)
        initial_pw = self._pw()
        user = get_user_model().objects.create_user(
            username=username,
            email=email,
            password=initial_pw,
        )

        uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        new_pw = self._pw()

        resp = self._password_reset_confirm(uidb64, token, new_pw)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        body = resp.json()
        self.assertEqual(body.get("status"), 0)
        self.assertEqual(body.get("message"), self.PW_RESET_CONFIRM_MESSAGE)

        user.refresh_from_db()
        self.assertTrue(user.check_password(new_pw))

    def test_password_reset_confirm_invalid_uid_returns_error(self) -> None:
        resp = self._password_reset_confirm("not-base64", "token", self._pw())
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

        body = resp.json()
        self.assertEqual(body.get("status"), 1)
        self.assertEqual(body.get("message"), "Invalid or expired reset link.")

    def test_password_reset_confirm_invalid_password_returns_error(
        self,
    ) -> None:
        username = self._user()
        email = self._email(username)
        user = get_user_model().objects.create_user(
            username=username,
            email=email,
            password=self._pw(),
        )

        uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)

        resp = self._password_reset_confirm(uidb64, token, "short")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

        body = resp.json()
        self.assertEqual(body.get("status"), 1)
        errors = self._as_dict(body.get("errors"), "errors")
        self.assertIn("new_password", errors)
        new_password_errors = cast(
            list[JSONValue], errors.get("new_password", [])
        )
        self.assertTrue(new_password_errors)
        message = cast(str, new_password_errors[0])
        self.assertIn("too short", message.lower())

    def test_password_reset_confirm_invalid_token_returns_error(self) -> None:
        username = self._user()
        email = self._email(username)
        user = get_user_model().objects.create_user(
            username=username,
            email=email,
            password=self._pw(),
        )

        uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
        resp = self._password_reset_confirm(
            uidb64, "invalid-token", self._pw()
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

        body = resp.json()
        self.assertEqual(body.get("status"), 1)
        self.assertEqual(body.get("message"), "Invalid or expired reset link.")
        self.assertIsNone(body.get("data"))

        errors = self._as_dict(body.get("errors"), "errors")
        self.assertEqual(
            errors.get("token"),
            ["Invalid or expired token."],
        )

    def test_password_reset_request_throttles(self) -> None:
        email = "missing@example.com"
        for _ in range(5):
            resp = self._password_reset_request(email)
            self.assertEqual(resp.status_code, status.HTTP_200_OK)

        resp = self._password_reset_request(email)
        self.assertEqual(resp.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(resp.json().get("status"), 1)

    def test_auth_backend_missing_login_or_password_returns_none(self) -> None:
        backend = UsernameOrEmailBackend()
        password = self._pw()
        self.assertIsNone(
            backend.authenticate(None, username=None, password=password)
        )
        self.assertIsNone(
            backend.authenticate(None, username="user", password=None)
        )

    def test_auth_backend_user_not_found_returns_none(self) -> None:
        backend = UsernameOrEmailBackend()
        password = self._pw()
        get_user_model().objects.create_user(
            username="existing",
            email="existing@example.com",
            password=password,
        )
        self.assertIsNone(
            backend.authenticate(None, username="missing", password=password)
        )

    def setUp(self) -> None:
        super().setUp()
        caches["default"].clear()
        if "throttle" in settings.CACHES:
            caches["throttle"].clear()
