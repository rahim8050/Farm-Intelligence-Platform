#!/usr/bin/env bash
set -euo pipefail

# Run Django ASGI with Daphne over TLS using Let's Encrypt certificates.
# Override via env vars as needed.
BIND_HOST="${BIND_HOST:-0.0.0.0}"
PORT="${PORT:-8001}"
CERT_DOMAIN="${CERT_DOMAIN:-}"
LE_LIVE_DIR="${LE_LIVE_DIR:-/etc/letsencrypt/live}"
ASGI_APP="${ASGI_APP:-config.asgi:application}"

if [[ -z "$CERT_DOMAIN" ]]; then
  echo "CERT_DOMAIN is required (example: CERT_DOMAIN=api.example.com)." >&2
  exit 1
fi

PRIVKEY_PATH="${PRIVKEY_PATH:-$LE_LIVE_DIR/$CERT_DOMAIN/privkey.pem}"
FULLCHAIN_PATH="${FULLCHAIN_PATH:-$LE_LIVE_DIR/$CERT_DOMAIN/fullchain.pem}"

if [[ ! -r "$PRIVKEY_PATH" ]]; then
  echo "Private key not readable: $PRIVKEY_PATH" >&2
  exit 1
fi

if [[ ! -r "$FULLCHAIN_PATH" ]]; then
  echo "Certificate chain not readable: $FULLCHAIN_PATH" >&2
  exit 1
fi

exec daphne \
  --endpoint "ssl:${PORT}:privateKey=${PRIVKEY_PATH}:certKey=${FULLCHAIN_PATH}:interface=${BIND_HOST}" \
  "$ASGI_APP"
