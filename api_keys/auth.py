from __future__ import annotations

import hmac
import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.models import AbstractBaseUser
from django.core.cache import caches
from django.core.cache.backends.base import BaseCache
from django.db.models import Q, QuerySet
from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.request import Request

from .models import ApiKey

API_KEY_PREFIX = "wk_live_"
PREFIX_LENGTH = 12
LAST_USED_AT_WRITE_INTERVAL = timedelta(minutes=5)

logger = logging.getLogger(__name__)


def generate_plaintext_key() -> str:
    return f"{API_KEY_PREFIX}{secrets.token_urlsafe(32)}"


def _peppered_secret(raw_key: str) -> str:
    return f"{settings.DJANGO_API_KEY_PEPPER}:{raw_key}"


def hash_api_key(plaintext_key: str) -> str:
    return make_password(_peppered_secret(plaintext_key))


def get_header_key(request: Request) -> str | None:
    header_value = request.META.get("HTTP_X_API_KEY")
    if header_value:
        return str(header_value)
    return None


def _eligible_keys(prefix: str, last4: str) -> QuerySet[ApiKey]:
    now = timezone.now()
    active_keys = ApiKey.objects.filter(
        prefix=prefix,
        last4=last4,
        revoked_at__isnull=True,
    ).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
    return active_keys.select_related("user")


def _auth_cache_alias() -> str:
    return str(getattr(settings, "API_KEY_AUTH_CACHE_ALIAS", "default"))


def _auth_cache_ttl_seconds() -> int:
    ttl = int(getattr(settings, "API_KEY_AUTH_CACHE_TTL_SECONDS", 60))
    return max(ttl, 0)


def _auth_cache() -> BaseCache:
    return caches[_auth_cache_alias()]


def _auth_cache_key(raw_key: str) -> str:
    digest = hmac.new(
        settings.DJANGO_API_KEY_PEPPER.encode("utf-8"),
        raw_key.encode("utf-8"),
        "sha256",
    ).hexdigest()
    return f"api_key.auth:{digest}"


def _load_cached_active_key(raw_key: str) -> ApiKey | None:
    ttl = _auth_cache_ttl_seconds()
    if ttl <= 0:
        return None

    cache_key = _auth_cache_key(raw_key)
    try:
        cached_key_id = _auth_cache().get(cache_key)
    except Exception as exc:  # pragma: no cover - cache backend fallback
        logger.debug("api_key.auth.cache.read_failed err=%s", exc)
        return None

    if not isinstance(cached_key_id, str):
        return None

    now = timezone.now()
    key = (
        ApiKey.objects.filter(
            id=cached_key_id,
            revoked_at__isnull=True,
        )
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        .select_related("user")
        .first()
    )
    if key is None:
        try:
            _auth_cache().delete(cache_key)
        except Exception as exc:  # pragma: no cover - cache backend fallback
            logger.debug("api_key.auth.cache.delete_failed err=%s", exc)
    return key


def _store_active_key_cache(raw_key: str, api_key: ApiKey) -> None:
    ttl = _auth_cache_ttl_seconds()
    if ttl <= 0:
        return
    try:
        _auth_cache().set(
            _auth_cache_key(raw_key),
            str(api_key.id),
            timeout=ttl,
        )
    except Exception as exc:  # pragma: no cover - cache backend fallback
        logger.debug("api_key.auth.cache.write_failed err=%s", exc)


def _client_ip(request: Request) -> str | None:
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded_for:
        parts = str(forwarded_for).split(",")
        if parts:
            return parts[0].strip() or None
    remote_addr = request.META.get("REMOTE_ADDR")
    return str(remote_addr) if remote_addr else None


def validate_api_key_with_reason(raw_key: str) -> tuple[ApiKey | None, str]:
    """Validate an API key and return a reason string for audit logging.

    The returned reason is one of:
    - "invalid" (bad format or no candidate keys)
    - "hash_mismatch" (prefix+last4 match exists, but hash check failed)
    - "revoked" (hash matches, but key is revoked)
    - "expired" (hash matches, but key is expired)
    - "ok" (valid)
    """

    if not raw_key or not raw_key.startswith(API_KEY_PREFIX):
        return None, "invalid"

    cached_key = _load_cached_active_key(raw_key)
    if cached_key is not None:
        return cached_key, "ok"

    prefix = raw_key[:PREFIX_LENGTH]
    last4 = raw_key[-4:]
    candidate_secret = _peppered_secret(raw_key)

    now = timezone.now()
    candidates = ApiKey.objects.filter(
        prefix=prefix, last4=last4
    ).select_related("user")
    found_candidate = False
    for key in candidates.iterator():
        found_candidate = True
        if check_password(candidate_secret, key.key_hash):
            if key.revoked_at is not None:
                return key, "revoked"
            if key.expires_at is not None and key.expires_at <= now:
                return key, "expired"
            _store_active_key_cache(raw_key, key)
            return key, "ok"
    if not found_candidate:
        return None, "invalid"
    return None, "hash_mismatch"


def validate_api_key(raw_key: str) -> ApiKey | None:
    api_key, reason = validate_api_key_with_reason(raw_key)
    return api_key if reason == "ok" else None


class ApiKeyAuthentication(BaseAuthentication):
    www_authenticate_realm = "api"

    def authenticate(
        self, request: Request
    ) -> tuple[AbstractBaseUser, ApiKey] | None:
        raw_key = get_header_key(request)
        if raw_key is None:
            logger.debug(
                "api_key.auth.missing path=%s method=%s ip=%s ua=%s",
                getattr(request, "path", ""),
                getattr(request, "method", ""),
                _client_ip(request),
                request.META.get("HTTP_USER_AGENT"),
            )
            return None

        api_key, reason = validate_api_key_with_reason(raw_key)
        if api_key is None or reason != "ok":
            logger.warning(
                "api_key.auth.failure reason=%s path=%s method=%s "
                "status_code=%s ip=%s ua=%s user_id=%s key_id=%s",
                reason if reason != "ok" else "invalid_credentials",
                getattr(request, "path", ""),
                getattr(request, "method", ""),
                401,
                _client_ip(request),
                request.META.get("HTTP_USER_AGENT"),
                getattr(api_key, "user_id", None) if api_key else None,
                getattr(api_key, "id", None) if api_key else None,
            )
            raise AuthenticationFailed("Invalid or expired API key.")

        if not api_key.user.is_active:
            logger.warning(
                "api_key.auth.failure reason=%s path=%s method=%s "
                "status_code=%s ip=%s ua=%s user_id=%s key_id=%s",
                "user_inactive",
                getattr(request, "path", ""),
                getattr(request, "method", ""),
                401,
                _client_ip(request),
                request.META.get("HTTP_USER_AGENT"),
                getattr(api_key, "user_id", None),
                getattr(api_key, "id", None),
            )
            raise AuthenticationFailed("User inactive or deleted.")

        now = timezone.now()
        cutoff = now - LAST_USED_AT_WRITE_INTERVAL
        ApiKey.objects.filter(id=api_key.id).filter(
            Q(last_used_at__isnull=True) | Q(last_used_at__lt=cutoff)
        ).update(last_used_at=now)

        if bool(getattr(settings, "API_KEY_AUTH_LOG_SUCCESS", True)):
            logger.info(
                "api_key.auth.success path=%s method=%s ip=%s ua=%s "
                "user_id=%s key_id=%s",
                getattr(request, "path", ""),
                getattr(request, "method", ""),
                _client_ip(request),
                request.META.get("HTTP_USER_AGENT"),
                getattr(api_key, "user_id", None),
                getattr(api_key, "id", None),
            )

        return api_key.user, api_key

    def authenticate_header(self, request: Request) -> str:
        return "X-API-Key"
