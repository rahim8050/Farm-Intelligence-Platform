# HMAC Clean Setup (DRF)

This guide shows how to generate a new HMAC client id + secret and load it into
`INTEGRATION_HMAC_CLIENTS_JSON` safely.

## 1) Generate a new client id + secret

Option A: management command (recommended)

```bash
python manage.py integrations_generate_hmac_client
```

Option B: local Python snippet (standard base64, with padding)

```bash
python - <<'PY'
import base64
import secrets
import uuid

client_id = str(uuid.uuid4())
secret_bytes = secrets.token_bytes(32)
secret_b64 = base64.b64encode(secret_bytes).decode("ascii")

print("CLIENT_ID=", client_id)
print("SECRET_B64=", secret_b64)
print(f"INTEGRATION_HMAC_CLIENTS_JSON='{{\"{client_id}\":\"{secret_b64}\"}}'")
PY
```

Notes:
- Use **standard base64** (alphabet `A-Z a-z 0-9 + /` with `=` padding).
- **Do not** use urlsafe base64 (`-` and `_`); DRF rejects it.
- Keep the secret private; do not commit it or paste it into logs.

## 2) Load into `.env`

Use dotenv-safe quoting (do not `source` `.env`; it is loaded by
`django-environ`/`python-dotenv`):

```dotenv
INTEGRATION_HMAC_CLIENTS_JSON='{"<CLIENT_ID>":"<SECRET_B64>"}'
```

If you already have other clients, merge the JSON object instead of replacing
it.

## 3) Verify DRF reads the env correctly

This snippet prints the presence, known client ids, and a **fingerprint** of the
secret (not the secret itself):

```bash
python - <<'PY'
import base64
import hashlib
import json
import os

raw = os.environ.get("INTEGRATION_HMAC_CLIENTS_JSON", "")
print("present=", bool(raw))
parsed = json.loads(raw or "{}")
print("client_ids=", sorted(parsed.keys()))

client_id = "<CLIENT_ID>"
secret_b64 = parsed.get(client_id, "")
secret_bytes = (
    base64.b64decode(secret_b64, validate=True) if secret_b64 else b""
)
secret_fpr = hashlib.sha256(secret_bytes).hexdigest()[:16] if secret_bytes else "missing"
print("secret_fpr=", secret_fpr)
PY
```

## 4) Configure the Nextcloud client

Set the Nextcloud app config for:
- `INTEGRATION_HMAC_CLIENT_ID`
- `INTEGRATION_HMAC_CLIENTS_JSON` (same base64 secret string)

See `docs/integration_auth.md` and `docs/security/nextcloud-hmac.md` for the
contract and header details.
