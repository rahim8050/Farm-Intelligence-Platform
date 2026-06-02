"""Authentication and account management API endpoints.

Authentication: some endpoints are public (AllowAny), others require JWT or API
key auth (global DRF settings).

All successful responses are wrapped by
`config.api.responses.success_response`:

    {"status": 0, "message": "<str>", "data": <object|null>, "errors": null}
"""

from __future__ import annotations

from typing import cast

from django.conf import settings
from django.contrib.auth import password_validation
from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.mail import send_mail
from django.utils.encoding import (
    DjangoUnicodeDecodeError,
    force_bytes,
    force_str,
)
from django.utils.http import (
    urlsafe_base64_decode,
    urlsafe_base64_encode,
)
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import (
    TokenRefreshView as SimpleJWTTokenRefresh,
)

from config.api.openapi import (
    error_envelope_serializer,
    success_envelope_serializer,
)
from config.api.responses import error_response, success_response

from .serializers import (
    LoginSerializer,
    MeSerializer,
    PasswordChangeSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    RegisterSerializer,
)

type JSONScalar = str | int | float | bool | None
type JSONValue = JSONScalar | list[JSONValue] | dict[str, JSONValue]

auth_error_response = error_envelope_serializer("AuthErrorResponse")

auth_tokens_schema = inline_serializer(
    name="AuthTokens",
    fields={
        "refresh": serializers.CharField(),
        "access": serializers.CharField(),
    },
)
auth_user_tokens_schema = inline_serializer(
    name="AuthUserTokens",
    fields={
        "user": MeSerializer(),
        "tokens": auth_tokens_schema,
    },
)
auth_success_response = success_envelope_serializer(
    "AuthSuccessResponse",
    data=auth_user_tokens_schema,
)

token_refresh_data_schema = inline_serializer(
    name="TokenRefreshData",
    fields={"access": serializers.CharField()},
)
token_refresh_success_response = success_envelope_serializer(
    "TokenRefreshSuccessResponse",
    data=token_refresh_data_schema,
)

me_success_response = success_envelope_serializer(
    "MeSuccessResponse",
    data=MeSerializer(),
)
password_change_success_response = success_envelope_serializer(
    "PasswordChangeSuccessResponse",
    data=serializers.JSONField(allow_null=True),
)
password_reset_request_success_response = success_envelope_serializer(
    "PasswordResetRequestSuccessResponse",
    data=serializers.JSONField(allow_null=True),
)
password_reset_confirm_success_response = success_envelope_serializer(
    "PasswordResetConfirmSuccessResponse",
    data=serializers.JSONField(allow_null=True),
)
password_reset_error_response = inline_serializer(
    name="PasswordResetErrorResponse",
    fields={
        "status": serializers.IntegerField(),
        "message": serializers.CharField(),
        "data": serializers.JSONField(allow_null=True),
        "errors": serializers.JSONField(allow_null=True),
    },
)

PASSWORD_RESET_REQUEST_MESSAGE = (
    "If an account exists for this email, a reset link has been sent."  # noqa: S105  # nosec B105
)
PASSWORD_RESET_CONFIRM_MESSAGE = (  # noqa: S105  # nosec B105
    "Password has been reset."  # noqa: S105  # nosec B105
)
PASSWORD_RESET_INVALID_MESSAGE = (  # noqa: S105  # nosec B105
    "Invalid or expired reset link."  # noqa: S105  # nosec B105
)


def _build_tokens(user: User) -> dict[str, str]:
    refresh = RefreshToken.for_user(user)
    return {"refresh": str(refresh), "access": str(refresh.access_token)}


def _as_json_dict(value: object) -> dict[str, JSONValue]:
    return cast(dict[str, JSONValue], value)


def _get_user_from_uid(uidb64: str) -> User | None:
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
    except (DjangoUnicodeDecodeError, TypeError, ValueError, OverflowError):
        return None
    return User.objects.filter(pk=uid).first()


@extend_schema(auth=[])
class RegisterView(APIView):
    """Create a new user account.

    Authentication: none.
    Permissions: AllowAny.
    Throttling: scope "register".
    Request body: `RegisterSerializer`.
    Success response: success envelope with `data.user` and `data.tokens`.
    """

    permission_classes = [AllowAny]
    throttle_scope = "register"

    @extend_schema(
        request=RegisterSerializer,
        responses={
            201: auth_success_response,
            400: auth_error_response,
        },
    )
    def post(self, request: Request) -> Response:
        """Register the user and return access/refresh tokens."""
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = cast(User, serializer.save())
        user_data = _as_json_dict(MeSerializer(user).data)
        tokens_data = cast(dict[str, JSONValue], _build_tokens(user))

        data: dict[str, JSONValue] = {
            "user": user_data,
            "tokens": tokens_data,
        }

        return success_response(
            data,
            message="Registered successfully",
            status_code=status.HTTP_201_CREATED,
        )


@extend_schema(auth=[])
class LoginView(APIView):
    """Authenticate a user and return JWT tokens.

    Authentication: none.
    Permissions: AllowAny.
    Throttling: scope "login".
    Request body: `LoginSerializer`.
    Success response: success envelope with `data.user` and `data.tokens`.
    """

    permission_classes = [AllowAny]
    throttle_scope = "login"

    @extend_schema(
        request=LoginSerializer,
        responses={
            200: auth_success_response,
            400: auth_error_response,
            401: auth_error_response,
        },
    )
    def post(self, request: Request) -> Response:
        """Validate credentials and return access/refresh tokens."""
        serializer = LoginSerializer(
            data=request.data,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)

        user = cast(User, serializer.validated_data["user"])
        user_data = _as_json_dict(MeSerializer(user).data)
        tokens_data = cast(dict[str, JSONValue], _build_tokens(user))

        data: dict[str, JSONValue] = {
            "user": user_data,
            "tokens": tokens_data,
        }

        return success_response(data, message="Login successful")


@extend_schema(auth=[])
class WrappedTokenRefreshView(SimpleJWTTokenRefresh):
    """Refresh a JWT access token.

    Authentication: none.
    Throttling: scope "token_refresh".
    Request body: SimpleJWT token refresh schema (refresh token).
    Success response: success envelope with `data.access`.
    """

    throttle_scope = "token_refresh"

    @extend_schema(
        request=TokenRefreshSerializer,
        responses={
            200: token_refresh_success_response,
            400: auth_error_response,
            401: auth_error_response,
        },
    )
    def post(
        self,
        request: Request,
        *args: object,
        **kwargs: object,
    ) -> Response:
        """Validate refresh token and return a new access token."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        access = cast(str, serializer.validated_data["access"])
        data: dict[str, JSONValue] = {"access": access}

        return success_response(data, message="Token refreshed")


@extend_schema(auth=cast(list[str], [{"BearerAuth": []}, {"ApiKeyAuth": []}]))
class MeView(APIView):
    """Return the authenticated user's profile.

    Authentication: BearerAuth (JWT) or ApiKeyAuth (X-API-Key).
    Permissions: IsAuthenticated.
    Request body: none.
    Success response: success envelope with `MeSerializer` data.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        responses={
            200: me_success_response,
            401: auth_error_response,
        }
    )
    def get(self, request: Request) -> Response:
        """Return the currently authenticated user's profile."""
        user = cast(User, request.user)
        data = _as_json_dict(MeSerializer(user).data)
        return success_response(data, message="User profile")


@extend_schema(auth=cast(list[str], [{"BearerAuth": []}, {"ApiKeyAuth": []}]))
class PasswordChangeView(APIView):
    """Change the authenticated user's password.

    Authentication: BearerAuth (JWT) or ApiKeyAuth (X-API-Key).
    Permissions: IsAuthenticated.
    Request body: `PasswordChangeSerializer`.
    Success response: success envelope with `data = null`.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=PasswordChangeSerializer,
        responses={
            200: password_change_success_response,
            400: auth_error_response,
            401: auth_error_response,
        },
    )
    def post(self, request: Request) -> Response:
        """Validate the old password and set the new password."""
        user = cast(User, request.user)
        serializer = PasswordChangeSerializer(
            data=request.data,
            context={"user": user},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return success_response(None, message="Password changed")


@extend_schema(auth=[])
class PasswordResetRequestView(APIView):
    """Send a password reset email for an account.

    Authentication: none.
    Permissions: AllowAny.
    Throttling: scope "password_reset".
    Request body: `PasswordResetRequestSerializer`.
    Success response: success envelope with `data = null`.
    """

    permission_classes = [AllowAny]
    throttle_scope = "password_reset"

    @extend_schema(
        request=PasswordResetRequestSerializer,
        responses={
            200: password_reset_request_success_response,
            400: password_reset_error_response,
        },
    )
    def post(self, request: Request) -> Response:
        """Inputs: email. Output: success envelope with data null.

        Side effects: sends a reset email for active users when configured.
        """
        serializer = PasswordResetRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "Request failed",
                errors=_as_json_dict(serializer.errors),
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        email = cast(str, serializer.validated_data["email"])
        user = User.objects.filter(email__iexact=email, is_active=True).first()
        if user is not None:
            uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
            token = default_token_generator.make_token(user)
            reset_link = (
                f"{settings.FRONTEND_RESET_URL}?uid={uidb64}&token={token}"
            )
            subject = "Password reset request"
            message = "\n".join(
                [
                    "You requested a password reset.",
                    "If you did not request this, you can ignore this email.",
                    "",
                    f"Reset your password: {reset_link}",
                ]
            )
            send_mail(
                subject,
                message,
                settings.DEFAULT_FROM_EMAIL,
                [user.email],
                fail_silently=True,
            )

        return success_response(
            None,
            message=PASSWORD_RESET_REQUEST_MESSAGE,
        )


@extend_schema(auth=[])
class PasswordResetConfirmView(APIView):
    """Confirm a password reset and set a new password.

    Authentication: none.
    Permissions: AllowAny.
    Throttling: scope "password_reset_confirm".
    Request body: `PasswordResetConfirmSerializer`.
    Success response: success envelope with `data = null`.
    """

    permission_classes = [AllowAny]
    throttle_scope = "password_reset_confirm"

    @extend_schema(
        request=PasswordResetConfirmSerializer,
        responses={
            200: password_reset_confirm_success_response,
            400: password_reset_error_response,
        },
    )
    def post(self, request: Request) -> Response:
        """Inputs: uid, token, new_password. Output: envelope with data null.

        Side effects: updates the user's password on success.
        """
        serializer = PasswordResetConfirmSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "Request failed",
                errors=_as_json_dict(serializer.errors),
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        uidb64 = cast(str, serializer.validated_data["uid"])
        token = cast(str, serializer.validated_data["token"])
        new_password = cast(str, serializer.validated_data["new_password"])

        user = _get_user_from_uid(uidb64)
        if (
            user is None
            or not user.is_active
            or not default_token_generator.check_token(user, token)
        ):
            invalid_errors: dict[str, JSONValue] = {
                "token": ["Invalid or expired token."]
            }
            return error_response(
                PASSWORD_RESET_INVALID_MESSAGE,
                errors=invalid_errors,
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        try:
            password_validation.validate_password(new_password, user)
        except DjangoValidationError as exc:
            errors: dict[str, JSONValue] = {"new_password": list(exc.messages)}
            return error_response(
                "Request failed",
                errors=errors,
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        user.set_password(new_password)
        user.save(update_fields=["password"])
        return success_response(
            None,
            message=PASSWORD_RESET_CONFIRM_MESSAGE,
        )
