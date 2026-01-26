from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from integrations.config import (
    IntegrationHMACConfigError,
    load_integration_hmac_clients,
)


def test_legacy_env_config_rejected_when_not_allowed(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "NEXTCLOUD_HMAC_CLIENTS_JSON",
        json.dumps({"legacy-client": "YWJj"}),
    )
    settings.INTEGRATION_LEGACY_CONFIG_ALLOWED = False
    settings.INTEGRATION_HMAC_CLIENTS_JSON = json.dumps({"nc-test-1": "YWJj"})

    with pytest.raises(IntegrationHMACConfigError) as exc:
        load_integration_hmac_clients()

    assert exc.value.code == "missing_config"


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (123, "bad_json"),
        ("", "missing_config"),
        ("   ", "missing_config"),
        ("{bad-json", "bad_json"),
        ('["not-an-object"]', "bad_json"),
        ("{}", "missing_config"),
        (json.dumps({"client": 123}), "bad_json"),
        (json.dumps({"": "YWJj"}), "bad_json"),
        (json.dumps({"client": " "}), "bad_json"),
        (
            json.dumps({" client ": "YWJj", "client": "YWJj"}),
            "bad_json",
        ),
        (json.dumps({"client": "not-base64"}), "bad_base64"),
    ],
)
def test_load_integration_hmac_clients_invalid_payloads(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    raw: object,
    code: str,
) -> None:
    monkeypatch.delenv("NEXTCLOUD_HMAC_CLIENTS_JSON", raising=False)
    settings.INTEGRATION_LEGACY_CONFIG_ALLOWED = True
    settings.INTEGRATION_HMAC_CLIENTS_JSON = raw

    with pytest.raises(IntegrationHMACConfigError) as exc:
        load_integration_hmac_clients()

    assert exc.value.code == code


def test_load_integration_hmac_clients_returns_decoded_secrets(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NEXTCLOUD_HMAC_CLIENTS_JSON", raising=False)
    settings.INTEGRATION_LEGACY_CONFIG_ALLOWED = True
    secret = base64.b64encode(b"shared-secret").decode("ascii")
    settings.INTEGRATION_HMAC_CLIENTS_JSON = json.dumps({"client-1": secret})

    clients = load_integration_hmac_clients()

    assert clients == {"client-1": b"shared-secret"}


def test_load_integration_hmac_clients_rejects_empty_decoded_bytes(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NEXTCLOUD_HMAC_CLIENTS_JSON", raising=False)
    settings.INTEGRATION_LEGACY_CONFIG_ALLOWED = True
    settings.INTEGRATION_HMAC_CLIENTS_JSON = json.dumps({"client-1": "YWJj"})

    monkeypatch.setattr(base64, "b64decode", lambda *_, **__: b"")

    with pytest.raises(IntegrationHMACConfigError) as exc:
        load_integration_hmac_clients()

    assert exc.value.code == "bad_base64"
