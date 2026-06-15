# Reverse Proxy for /api/v1/

This document covers serving the Django + DRF API behind a TLS-terminating
reverse proxy (Nginx or Apache), the required proxy headers, and how this
supports the Nextcloud client. All examples use placeholders and keep the
existing `/api/v1/` base path unchanged.

## Architecture

```
Client (Nextcloud or other) -> Nginx/Apache reverse proxy -> Django + DRF app
```

- The proxy terminates TLS and forwards traffic to the Django app.
- The `/api/v1/` prefix is preserved end to end; do not rewrite it.

## Required proxy headers (trusted)

These headers let Django understand the original request as seen by the client.
Only trust them if they are set by your reverse proxy (strip any incoming
`X-Forwarded-*` from the public edge).

| Header | Why it matters |
| --- | --- |
| `Host` | Ensures Django sees the public hostname for `ALLOWED_HOSTS` and URL construction. |
| `X-Forwarded-Proto` | Tells Django the original scheme (http/https) for secure cookies and CSRF logic. |
| `X-Forwarded-For` | Preserves client IPs for logging, audit, and throttling. |

## Reverse proxy examples (minimal)

Nginx (keeps `/api/v1/` untouched and sets required headers):

```nginx
upstream farm_intelligence_platform {
  server <internal-host>:<internal-port>;
}

server {
  server_name api.example.com;

  location /api/v1/ {
    proxy_pass http://farm_intelligence_platform;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
  }
}
```

Apache (conceptual, adjust to your deployment):

```apache
ProxyPreserveHost On
ProxyAddHeaders On
RequestHeader set X-Forwarded-Proto "https"

ProxyPass /api/v1/ http://<internal-host>:<internal-port>/api/v1/
ProxyPassReverse /api/v1/ http://<internal-host>:<internal-port>/api/v1/
```

## Django proxy-awareness settings (examples only)

These are examples, not hard-coded values. Only enable them if your reverse
proxy sets the matching headers and you trust that proxy.

```python
# config/settings.py (examples only)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True  # Only if your proxy sets X-Forwarded-Host
CSRF_TRUSTED_ORIGINS = ["https://api.example.com"]
ALLOWED_HOSTS = ["api.example.com"]
```

## Nextcloud client considerations

Nextcloud request signing (HMAC) uses method, path, query, and body. To avoid
signature mismatches:

- Preserve the `/api/v1/` prefix and avoid path rewrites or trailing-slash
  normalization.
- Preserve the raw query string and request body bytes (do not modify them).
- Keep the public hostname stable so `Host` and `X-Forwarded-Proto` reflect the
  URL configured in Nextcloud.

Reference: `docs/security/nextcloud-hmac.md`.

## Block schema/docs endpoints at the proxy

Schema and Swagger/ReDoc endpoints are useful for internal development but can
expose implementation details publicly. Consider restricting them at the edge.

Nginx (deny all or allow only a trusted CIDR):

```nginx
location = /api/schema/ { deny all; }
location = /api/docs/ { deny all; }
location = /api/redoc/ { deny all; }
```

Apache:

```apache
<Location "/api/schema/">
  Require all denied
</Location>
<Location "/api/docs/">
  Require all denied
</Location>
<Location "/api/redoc/">
  Require all denied
</Location>
```

## Verification (through the proxy)

Use curl against the public base URL and confirm expected status codes and
JSON content types:

```bash
curl -sS -D - -o /dev/null https://api.example.com/api/v1/integrations/ping/
curl -sS -D - -o /dev/null \
  -H "X-API-Key: <api-key>" \
  https://api.example.com/api/v1/integrations/ping/
```

For Nextcloud HMAC checks, follow the signing instructions in
`docs/security/nextcloud-hmac.md` and call
`/api/v1/integrations/nextcloud/ping/`.

Legacy aliases under `/api/v1/integration/` remain available but are
deprecated.

## Proxy hardening recommendations

- Add rate limiting at the proxy (for example, `limit_req` in Nginx) even if
  application throttling is enabled.
- Set reasonable timeouts (`proxy_read_timeout`, `proxy_send_timeout`) to avoid
  long-held connections.
