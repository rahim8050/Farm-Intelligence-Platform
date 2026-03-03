"""Integration HMAC configuration loader.

Loads INTEGRATION_HMAC_CLIENTS_JSON and validates strict base64 secrets.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from functools import lru_cache

from django.conf import settings


class IntegrationHMACConfigError(Exception):
    """Raised when integration HMAC configuration is missing or invalid."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@lru_cache(maxsize=16)
def _load_clients_cached(
    *,
    raw: str,
    legacy_allowed: bool,
    legacy_raw: str,
) -> tuple[tuple[str, bytes], ...]:
    if legacy_raw and not legacy_allowed:
        raise IntegrationHMACConfigError(
            "Legacy NEXTCLOUD_HMAC_CLIENTS_JSON is not allowed; set "
            "INTEGRATION_HMAC_CLIENTS_JSON and remove the legacy variable "
            "(or temporarily set INTEGRATION_LEGACY_CONFIG_ALLOWED=true).",
            code="missing_config",
        )

    if not raw:
        raise IntegrationHMACConfigError(
            "INTEGRATION_HMAC_CLIENTS_JSON is required.",
            code="missing_config",
        )

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise IntegrationHMACConfigError(
            "INTEGRATION_HMAC_CLIENTS_JSON must be valid JSON.",
            code="bad_json",
        ) from exc

    if not isinstance(parsed, dict):
        raise IntegrationHMACConfigError(
            "INTEGRATION_HMAC_CLIENTS_JSON must be a JSON object.",
            code="bad_json",
        )
    if not parsed:
        raise IntegrationHMACConfigError(
            "INTEGRATION_HMAC_CLIENTS_JSON must not be empty.",
            code="missing_config",
        )

    clients: dict[str, bytes] = {}
    for raw_client_id, raw_secret in parsed.items():
        if not isinstance(raw_client_id, str) or not isinstance(
            raw_secret, str
        ):
            raise IntegrationHMACConfigError(
                "INTEGRATION_HMAC_CLIENTS_JSON must map strings to strings.",
                code="bad_json",
            )

        client_id = raw_client_id.strip()
        secret_b64 = raw_secret.strip()
        if not client_id or not secret_b64:
            raise IntegrationHMACConfigError(
                "INTEGRATION_HMAC_CLIENTS_JSON cannot contain empty keys "
                "or values.",
                code="bad_json",
            )
        if client_id in clients:
            raise IntegrationHMACConfigError(
                "INTEGRATION_HMAC_CLIENTS_JSON contains duplicate client_id "
                "entries after trimming.",
                code="bad_json",
            )

        try:
            secret_bytes = base64.b64decode(secret_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise IntegrationHMACConfigError(
                "INTEGRATION_HMAC_CLIENTS_JSON entry for client_id "
                f"'{client_id}' is not valid base64.",
                code="bad_base64",
            ) from exc
        if not secret_bytes:
            raise IntegrationHMACConfigError(
                "INTEGRATION_HMAC_CLIENTS_JSON entry for client_id "
                f"'{client_id}' decodes to empty bytes.",
                code="bad_base64",
            )

        clients[client_id] = secret_bytes

    return tuple(clients.items())


def clear_integration_hmac_clients_cache() -> None:
    """Clear parsed HMAC clients cache (useful for tests)."""

    _load_clients_cached.cache_clear()


def load_integration_hmac_clients() -> dict[str, bytes]:
    """Return validated client secrets as {client_id: secret_bytes}."""

    legacy_allowed = bool(
        getattr(settings, "INTEGRATION_LEGACY_CONFIG_ALLOWED", False)
    )
    legacy_raw = os.environ.get("NEXTCLOUD_HMAC_CLIENTS_JSON", "").strip()

    raw_setting = getattr(settings, "INTEGRATION_HMAC_CLIENTS_JSON", "")
    if not isinstance(raw_setting, str):
        raise IntegrationHMACConfigError(
            "INTEGRATION_HMAC_CLIENTS_JSON must be a string.",
            code="bad_json",
        )

    raw = raw_setting.strip()
    pairs = _load_clients_cached(
        raw=raw,
        legacy_allowed=legacy_allowed,
        legacy_raw=legacy_raw,
    )
    return dict(pairs)
