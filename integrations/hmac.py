"""Nextcloud instance-level HMAC request signing helpers.

This module implements request-integrity verification and replay protection for
Nextcloud -> DRF calls using a shared-secret HMAC contract.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote

from django.conf import settings
from django.core.cache import caches
from rest_framework.request import Request

from .config import IntegrationHMACConfigError, load_integration_hmac_clients

NEXTCLOUD_CLIENT_ID_HEADER = "X-NC-CLIENT-ID"
NEXTCLOUD_TIMESTAMP_HEADER = "X-NC-TIMESTAMP"
NEXTCLOUD_NONCE_HEADER = "X-NC-NONCE"
NEXTCLOUD_SIGNATURE_HEADER = "X-NC-SIGNATURE"
INTEGRATIONS_CLIENT_ID_HEADER = "X-Client-Id"
INTEGRATIONS_TIMESTAMP_HEADER = "X-Timestamp"
INTEGRATIONS_NONCE_HEADER = "X-Nonce"
INTEGRATIONS_SIGNATURE_HEADER = "X-Signature"

_RFC3986_SAFE = "-_.~"
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class NextcloudHMACHeaders:
    """Parsed Nextcloud signature headers."""

    client_id: str
    timestamp: int
    nonce: str
    signature: str


class NextcloudHMACVerificationError(Exception):
    """Raised when a request fails Nextcloud HMAC verification."""

    def __init__(self, message: str, *, code: str = "sig_mismatch") -> None:
        super().__init__(message)
        self.code = code


def canonicalize_query(query_string: str) -> str:
    """Parse, sort, and re-encode a query string; preserve duplicates."""

    if not query_string:
        return ""

    pairs = parse_qsl(
        query_string,
        keep_blank_values=True,
        strict_parsing=False,
        separator="&",
    )
    encoded_pairs = [
        (quote(k, safe=_RFC3986_SAFE), quote(v, safe=_RFC3986_SAFE))
        for k, v in pairs
    ]
    encoded_pairs.sort(key=lambda item: (item[0], item[1]))
    return "&".join(f"{k}={v}" for k, v in encoded_pairs)


def body_sha256_hex(*, method: str, body: bytes) -> str:
    """Compute sha256 hex of raw request body (empty bytes for GET)."""

    if method.upper() == "GET":
        body = b""
    return hashlib.sha256(body).hexdigest()


def build_canonical_string(
    *,
    method: str,
    path: str,
    query_string: str,
    timestamp: int,
    nonce: str,
    body_sha256: str,
) -> str:
    """Build the newline-separated canonical string for verification."""

    return "\n".join(
        [
            method.upper(),
            path,
            canonicalize_query(query_string),
            str(timestamp),
            nonce,
            body_sha256,
        ]
    )


def compute_hmac_signature_hex(*, secret: bytes, canonical_string: str) -> str:
    """Compute the expected hex HMAC-SHA256 signature."""

    digest = hmac.new(
        secret,
        canonical_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return digest


def compute_hmac_signature_b64(*, secret: bytes, canonical_string: str) -> str:
    """Compute the expected base64 HMAC-SHA256 signature (Nextcloud format)."""

    digest = hmac.new(
        secret,
        canonical_string.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode()


def _log_hmac_debug(
    *,
    client_id: str,
    method: str,
    path: str,
    body_sha256: str,
    canonical: str,
    signature: str,
    expected_signature: str,
    secret: bytes,
) -> None:
    if not getattr(settings, "NEXTCLOUD_HMAC_DEBUG_LOGGING", False):
        return

    canonical_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    logger.debug(
        "nextcloud_hmac.debug client_id=%s method=%s path=%s "
        "body_sha256=%s canonical_sha256=%s "
        "signature=%s expected_signature=%s",
        client_id,
        method.upper(),
        path,
        body_sha256,
        canonical_hash,
        signature,
        expected_signature,
    )


def _get_required_headers(request: Request) -> NextcloudHMACHeaders:
    client_id = request.headers.get(NEXTCLOUD_CLIENT_ID_HEADER)
    if not client_id:
        client_id = request.headers.get(INTEGRATIONS_CLIENT_ID_HEADER)
    timestamp_raw = request.headers.get(NEXTCLOUD_TIMESTAMP_HEADER)
    if not timestamp_raw:
        timestamp_raw = request.headers.get(INTEGRATIONS_TIMESTAMP_HEADER)
    nonce = request.headers.get(NEXTCLOUD_NONCE_HEADER)
    if not nonce:
        nonce = request.headers.get(INTEGRATIONS_NONCE_HEADER)
    signature = request.headers.get(NEXTCLOUD_SIGNATURE_HEADER)
    if not signature:
        signature = request.headers.get(INTEGRATIONS_SIGNATURE_HEADER)

    if not client_id or not timestamp_raw or not nonce or not signature:
        raise NextcloudHMACVerificationError(
            "Missing Nextcloud HMAC headers",
            code="missing_headers",
        )

    try:
        timestamp = int(timestamp_raw)
    except ValueError as exc:
        raise NextcloudHMACVerificationError(
            "Invalid Nextcloud timestamp header",
            code="skew",
        ) from exc

    return NextcloudHMACHeaders(
        client_id=client_id,
        timestamp=timestamp,
        nonce=nonce,
        signature=signature,
    )


def verify_nextcloud_hmac_request(
    request: Request,
    *,
    nonce_ttl_seconds: int | None = None,
    allowed_methods: list[str] | tuple[str, ...] | None = None,
) -> str:
    """Verify request signature + replay protection; return `client_id`.

    Raises `NextcloudHMACVerificationError` on any verification failure.
    `allowed_methods` is used to classify method-mismatch failures.
    """

    if not getattr(settings, "NEXTCLOUD_HMAC_ENABLED", True):
        client_id = request.headers.get(INTEGRATIONS_CLIENT_ID_HEADER, "")
        if not client_id:
            client_id = request.headers.get(NEXTCLOUD_CLIENT_ID_HEADER, "")
        return client_id

    headers = _get_required_headers(request)

    try:
        clients = load_integration_hmac_clients()
    except IntegrationHMACConfigError as exc:
        raise NextcloudHMACVerificationError(
            str(exc),
            code=exc.code,
        ) from exc

    secret = clients.get(headers.client_id)
    if secret is None:
        raise NextcloudHMACVerificationError(
            "Unknown Nextcloud client_id",
            code="unknown_client",
        )

    now = int(time.time())
    max_skew = int(getattr(settings, "NEXTCLOUD_HMAC_MAX_SKEW_SECONDS", 300))
    if headers.timestamp - now > max_skew:
        raise NextcloudHMACVerificationError(
            "Nextcloud timestamp outside skew window",
            code="skew",
        )
    if now - headers.timestamp > max_skew:
        raise NextcloudHMACVerificationError(
            "Nextcloud timestamp outside skew window",
            code="skew",
        )

    method = request.method
    if not method:
        raise NextcloudHMACVerificationError(
            "Invalid request method",
            code="method_mismatch",
        )

    body_sha256 = body_sha256_hex(
        method=method,
        body=request.body,
    )
    canonical = build_canonical_string(
        method=method,
        path=request.path,
        query_string=request.META.get("QUERY_STRING", ""),
        timestamp=headers.timestamp,
        nonce=headers.nonce,
        body_sha256=body_sha256,
    )
    expected_sig = compute_hmac_signature_b64(
        secret=secret,
        canonical_string=canonical,
    )
    _log_hmac_debug(
        client_id=headers.client_id,
        method=method,
        path=request.path,
        body_sha256=body_sha256,
        canonical=canonical,
        signature=headers.signature,
        expected_signature=expected_sig,
        secret=secret,
    )
    if not hmac.compare_digest(headers.signature, expected_sig):
        normalized_method = method.upper()

        if allowed_methods:
            for candidate_method in allowed_methods:
                candidate = candidate_method.upper()
                if candidate == normalized_method:
                    continue
                candidate_canonical = build_canonical_string(
                    method=candidate,
                    path=request.path,
                    query_string=request.META.get("QUERY_STRING", ""),
                    timestamp=headers.timestamp,
                    nonce=headers.nonce,
                    body_sha256=body_sha256_hex(
                        method=candidate,
                        body=request.body,
                    ),
                )
                candidate_sig = compute_hmac_signature_b64(
                    secret=secret,
                    canonical_string=candidate_canonical,
                )
                if hmac.compare_digest(headers.signature, candidate_sig):
                    raise NextcloudHMACVerificationError(
                        "Nextcloud signature mismatch (method).",
                        code="method_mismatch",
                    )

        path_variants: list[str] = []
        if request.path.endswith("/"):
            path_variants.append(request.path.rstrip("/"))
        else:
            path_variants.append(f"{request.path}/")
        for candidate_path in path_variants:
            candidate_canonical = build_canonical_string(
                method=method,
                path=candidate_path,
                query_string=request.META.get("QUERY_STRING", ""),
                timestamp=headers.timestamp,
                nonce=headers.nonce,
                body_sha256=body_sha256_hex(
                    method=method,
                    body=request.body,
                ),
            )
            candidate_sig = compute_hmac_signature_b64(
                secret=secret,
                canonical_string=candidate_canonical,
            )
            if hmac.compare_digest(headers.signature, candidate_sig):
                raise NextcloudHMACVerificationError(
                    "Nextcloud signature mismatch (path).",
                    code="path_mismatch",
                )

        if normalized_method == "GET":
            if request.body:
                body_hash = hashlib.sha256(request.body).hexdigest()
                candidate_canonical = build_canonical_string(
                    method=method,
                    path=request.path,
                    query_string=request.META.get("QUERY_STRING", ""),
                    timestamp=headers.timestamp,
                    nonce=headers.nonce,
                    body_sha256=body_hash,
                )
                candidate_sig = compute_hmac_signature_b64(
                    secret=secret,
                    canonical_string=candidate_canonical,
                )
                if hmac.compare_digest(headers.signature, candidate_sig):
                    raise NextcloudHMACVerificationError(
                        "Nextcloud signature mismatch (body hash).",
                        code="body_hash_mismatch",
                    )
        else:
            empty_body_hash = hashlib.sha256(b"").hexdigest()
            candidate_canonical = build_canonical_string(
                method=method,
                path=request.path,
                query_string=request.META.get("QUERY_STRING", ""),
                timestamp=headers.timestamp,
                nonce=headers.nonce,
                body_sha256=empty_body_hash,
            )
            candidate_sig = compute_hmac_signature_b64(
                secret=secret,
                canonical_string=candidate_canonical,
            )
            if hmac.compare_digest(headers.signature, candidate_sig):
                raise NextcloudHMACVerificationError(
                    "Nextcloud signature mismatch (body hash).",
                    code="body_hash_mismatch",
                )

        raise NextcloudHMACVerificationError(
            "Invalid Nextcloud signature",
            code="sig_mismatch",
        )

    cache_alias = str(
        getattr(settings, "NEXTCLOUD_HMAC_CACHE_ALIAS", "default")
    )
    if nonce_ttl_seconds is None:
        nonce_ttl = int(
            getattr(settings, "NEXTCLOUD_HMAC_NONCE_TTL_SECONDS", 360)
        )
    else:
        nonce_ttl = int(nonce_ttl_seconds)
    cache = caches[cache_alias]
    cache_key = f"nc_hmac:{headers.client_id}:{headers.nonce}"
    if not cache.add(cache_key, 1, timeout=nonce_ttl):
        raise NextcloudHMACVerificationError(
            "Nextcloud nonce replay detected",
            code="replay",
        )

    return headers.client_id
