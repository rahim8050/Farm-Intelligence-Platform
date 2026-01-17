# Proxy Headers for Nextcloud Integration

When Nextcloud calls the DRF backend through an nginx/Apache proxy, the proxy
must preserve authorization + HMAC headers exactly as received. If these
headers are stripped or rewritten, token minting and diagnostics requests will
fail.

## Required pass-through headers

Always forward these from Nextcloud to DRF:

- `Authorization` (Bearer token for `/integrations/nextcloud/status/` + `/preview.png`)
- `X-API-Key` (token bootstrap only)
- `X-Client-Id` (integration client id)
- `X-Timestamp`
- `X-Nonce`
- `X-Signature`
- `X-Request-Id` (optional but recommended for traceability)

If you still use the legacy Nextcloud ping header names, also forward:

- `X-NC-CLIENT-ID`
- `X-NC-TIMESTAMP`
- `X-NC-NONCE`
- `X-NC-SIGNATURE`

Also forward the standard proxy headers so Django can reconstruct the original
request:

- `Host`
- `X-Forwarded-Proto`
- `X-Forwarded-For`

## Nginx example

```nginx
location /api/v1/ {
  proxy_pass http://weather_apis;
  proxy_set_header Host $host;
  proxy_set_header X-Forwarded-Proto $scheme;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

  proxy_set_header Authorization $http_authorization;
  proxy_set_header X-API-Key $http_x_api_key;
  proxy_set_header X-Client-Id $http_x_client_id;
  proxy_set_header X-Timestamp $http_x_timestamp;
  proxy_set_header X-Nonce $http_x_nonce;
  proxy_set_header X-Signature $http_x_signature;
  proxy_set_header X-Request-Id $http_x_request_id;

  proxy_set_header X-NC-CLIENT-ID $http_x_nc_client_id;
  proxy_set_header X-NC-TIMESTAMP $http_x_nc_timestamp;
  proxy_set_header X-NC-NONCE $http_x_nc_nonce;
  proxy_set_header X-NC-SIGNATURE $http_x_nc_signature;
}
```

## Apache example (conceptual)

```apache
ProxyPreserveHost On
ProxyAddHeaders On
RequestHeader set X-Forwarded-Proto "https"

RequestHeader set Authorization "%{HTTP:Authorization}e"
RequestHeader set X-API-Key "%{HTTP:X-API-Key}e"
RequestHeader set X-Client-Id "%{HTTP:X-Client-Id}e"
RequestHeader set X-Timestamp "%{HTTP:X-Timestamp}e"
RequestHeader set X-Nonce "%{HTTP:X-Nonce}e"
RequestHeader set X-Signature "%{HTTP:X-Signature}e"
RequestHeader set X-Request-Id "%{HTTP:X-Request-Id}e"

RequestHeader set X-NC-CLIENT-ID "%{HTTP:X-NC-CLIENT-ID}e"
RequestHeader set X-NC-TIMESTAMP "%{HTTP:X-NC-TIMESTAMP}e"
RequestHeader set X-NC-NONCE "%{HTTP:X-NC-NONCE}e"
RequestHeader set X-NC-SIGNATURE "%{HTTP:X-NC-SIGNATURE}e"

ProxyPass /api/v1/ http://<internal-host>:<internal-port>/api/v1/
ProxyPassReverse /api/v1/ http://<internal-host>:<internal-port>/api/v1/
```

## Notes

- Preserve the `/api/v1/` path and raw query string; HMAC signing depends on
  exact method/path/query/body.
- If your proxy strips `Authorization` by default, explicitly forward it as
  shown above.
