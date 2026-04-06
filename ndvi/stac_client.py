"""STAC client utilities for NDVI item selection and raster processing."""

from __future__ import annotations

import importlib
import logging
import math
import os
import random
import tempfile
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from typing import Any, Final
from urllib.parse import urljoin

import httpx
import numpy as np
from django.conf import settings

from ndvi.engines.base import BBox

MAX_ERROR_SNIPPET_CHARS = 1600
DEFAULT_MAX_ITEMS = 500
DEFAULT_LIMIT = 200
DEFAULT_STATS_SAMPLE_SIZE = 128
DEFAULT_STAC_API_URL: Final[str] = "https://stac.dataspace.copernicus.eu/v1/"
ASSET_RESOLUTION_ORDER: Final[tuple[str, ...]] = ("10m", "20m", "60m")

logger = logging.getLogger(__name__)

StacBBox = tuple[float, float, float, float]


def _validate_stac_bbox_values(values: StacBBox) -> bool:
    min_lon, min_lat, max_lon, max_lat = values
    if not all(math.isfinite(value) for value in values):
        return False
    if min_lon < -180.0 or max_lon > 180.0:
        return False
    if min_lat < -90.0 or max_lat > 90.0:
        return False
    return min_lon < max_lon and min_lat < max_lat


def _looks_like_lat_lon_order(values: StacBBox) -> bool:
    first_lat_like = abs(values[0]) <= 90.0 and abs(values[2]) <= 90.0
    second_lon_like = abs(values[1]) <= 180.0 and abs(values[3]) <= 180.0
    if not (first_lat_like and second_lon_like):
        return False
    return abs(values[0]) < abs(values[1]) and abs(values[2]) < abs(values[3])


def _coerce_bbox_values(
    bbox: BBox | tuple[float, float, float, float] | list[float],
) -> StacBBox:
    if isinstance(bbox, BBox):
        return (
            float(bbox.west),
            float(bbox.south),
            float(bbox.east),
            float(bbox.north),
        )
    if len(bbox) != 4:
        raise ValueError("bbox must contain exactly 4 values.")
    return (
        float(bbox[0]),
        float(bbox[1]),
        float(bbox[2]),
        float(bbox[3]),
    )


def normalize_stac_bbox(
    bbox: BBox | tuple[float, float, float, float] | list[float],
    *,
    farm_id: int | None = None,
    job_id: int | None = None,
    log_on_swap: bool = True,
) -> StacBBox:
    """Return STAC bbox ordering: (min_lon, min_lat, max_lon, max_lat)."""

    raw = _coerce_bbox_values(bbox)
    lon_lat = raw
    lat_lon = (raw[1], raw[0], raw[3], raw[2])
    lon_lat_valid = _validate_stac_bbox_values(lon_lat)
    lat_lon_valid = _validate_stac_bbox_values(lat_lon)
    swapped = False

    if lon_lat_valid and lat_lon_valid:
        if _looks_like_lat_lon_order(raw):
            resolved = lat_lon
            swapped = True
        else:
            resolved = lon_lat
    elif lon_lat_valid:
        resolved = lon_lat
    elif lat_lon_valid:
        resolved = lat_lon
        swapped = True
    else:
        raise ValueError(
            "Invalid bbox; expected lon/lat values in valid ranges "
            "with min<max."
        )

    if swapped and log_on_swap:
        logger.warning(
            "ndvi.stac.bbox_swapped farm_id=%s job_id=%s "
            "bbox_in=%s bbox_out=%s",
            farm_id if farm_id is not None else "-",
            job_id if job_id is not None else "-",
            raw,
            resolved,
        )
    return resolved


class StacError(RuntimeError):
    """Base error for STAC failures."""


class StacUpstreamError(StacError):
    """Raised when the STAC API cannot be reached or returns errors."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        self.status_code = status_code
        self.retryable = retryable
        super().__init__(message)


class StacWafBlockedError(StacUpstreamError):
    """Raised when a WAF (Web Application Firewall) blocks the request.

    This error is non-retryable and indicates the IP has been blocked
    by an upstream firewall (e.g., F5 BIG-IP, Cloudflare).
    Retrying will only produce the same result until the ban expires.
    """

    def __init__(
        self,
        message: str,
        *,
        support_id: str | None = None,
    ) -> None:
        super().__init__(message, retryable=False)
        self.support_id = support_id


class _CircuitBreaker:
    """Simple circuit breaker to stop retrying when the upstream is blocked.

    States:
      - CLOSED: Normal operation, requests pass through.
      - OPEN: Circuit is tripped, all requests fail immediately.
      - HALF_OPEN: Testing if the upstream has recovered.

    The circuit opens after `failure_threshold` consecutive failures
    and closes again after one successful request in HALF_OPEN state.
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        reset_timeout_secs: float = 300.0,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._reset_timeout_secs = reset_timeout_secs
        self._state = self.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0

    @property
    def state(self) -> str:
        """Return current state, auto-transitioning OPEN→HALF_OPEN on
        timeout."""
        if self._state == self.OPEN:
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed >= self._reset_timeout_secs:
                self._state = self.HALF_OPEN
                logger.info(
                    "STAC circuit breaker: OPEN→HALF_OPEN after %.0fs",
                    elapsed,
                )
        return self._state

    def record_success(self) -> None:
        """Record a successful request."""
        self._failure_count = 0
        if self._state == self.HALF_OPEN:
            self._state = self.CLOSED
            logger.info("STAC circuit breaker: HALF_OPEN→CLOSED (recovered)")

    def record_failure(self) -> None:
        """Record a failed request."""
        self._failure_count += 1
        self._last_failure_time = time.monotonic()

        if self._state == self.HALF_OPEN:
            self._state = self.OPEN
            logger.warning(
                "STAC circuit breaker: HALF_OPEN→OPEN (upstream still down)"
            )
        elif self._failure_count >= self._failure_threshold:
            self._state = self.OPEN
            logger.warning(
                "STAC circuit breaker: CLOSED→OPEN after %d failures",
                self._failure_count,
            )

    def is_open(self) -> bool:
        """Check if the circuit is open (should block requests)."""
        return self.state == self.OPEN

    def check_state(self) -> None:
        """Raise CircuitOpenError if the circuit is open.

        This method is intentionally a no-op here; the caller checks
        `is_open()` and raises `StacUpstreamError` with a clear message
        to avoid introducing a new exception type into task handlers.
        """


class StacProcessingError(StacError):
    """Raised when raster processing fails."""


class StacDependencyError(StacError):
    """Raised when optional STAC raster dependencies are missing."""


@dataclass(frozen=True)
class StacItem:
    """Parsed STAC item metadata needed for NDVI extraction."""

    id: str
    datetime: datetime
    assets: dict[str, str]
    cloud_cover: float | None
    collection: str | None = None

    @property
    def date(self) -> date:
        return self.datetime.date()


@dataclass(frozen=True)
class NdviStats:
    """Computed NDVI statistics."""

    mean: float
    min: float
    max: float
    sample_count: int


def filter_items_by_cloud(
    items: Iterable[StacItem],
    max_cloud: int,
) -> list[StacItem]:
    """Filter items by cloud cover when present."""

    filtered: list[StacItem] = []
    for item in items:
        if item.cloud_cover is None or item.cloud_cover <= max_cloud:
            filtered.append(item)
    return filtered


def select_best_item(
    items: Iterable[StacItem],
    *,
    target_date: date,
    window_days: int,
) -> StacItem | None:
    """Select the best item by lowest cloud then closest date."""

    candidates: list[tuple[float, int, datetime, StacItem]] = []
    for item in items:
        delta_days = abs((item.date - target_date).days)
        if delta_days > window_days:
            continue
        cloud_rank = (
            item.cloud_cover if item.cloud_cover is not None else 101.0
        )
        candidates.append((cloud_rank, delta_days, item.datetime, item))
    if not candidates:
        return None
    candidates.sort(key=lambda row: (row[0], row[1], row[2]))
    return candidates[0][3]


def build_asset_fallbacks(asset_name: str) -> list[str]:
    """Build fallback asset keys for a base name (prefers 10m)."""

    normalized = asset_name.strip()
    if not normalized:
        return []
    base = normalized.split("_", 1)[0]
    candidates = [f"{base}_{suffix}" for suffix in ASSET_RESOLUTION_ORDER]
    candidates.append(base)
    fallbacks: list[str] = []
    for candidate in candidates:
        if not candidate or candidate == normalized:
            continue
        if candidate not in fallbacks:
            fallbacks.append(candidate)
    return fallbacks


def build_asset_candidates(asset_name: str) -> list[str]:
    """Build asset candidates in lookup order (configured, then fallbacks)."""

    normalized = asset_name.strip()
    if not normalized:
        return []
    candidates = [normalized]
    for fallback in build_asset_fallbacks(normalized):
        if fallback not in candidates:
            candidates.append(fallback)
    return candidates


def resolve_asset_href_candidates(
    item: StacItem, candidates: Iterable[str]
) -> str | None:
    """Resolve the first matching asset href from a candidate list."""

    asset_map = item.assets
    lowered_assets = {key.lower(): href for key, href in asset_map.items()}
    for candidate in candidates:
        if not candidate:
            continue
        if candidate in asset_map:
            return asset_map[candidate]
        lowered = candidate.lower()
        if lowered in lowered_assets:
            return lowered_assets[lowered]
    return None


def resolve_asset_href(
    item: StacItem,
    asset_name: str,
    *,
    fallback_names: Iterable[str] | None = None,
) -> str | None:
    """Resolve an asset href by name (case-insensitive) with fallbacks."""

    candidates = [asset_name]
    if fallback_names:
        for fallback in fallback_names:
            if fallback not in candidates:
                candidates.append(fallback)
    return resolve_asset_href_candidates(item, candidates)


def normalize_cloud_fraction(cloud_cover: float | None) -> float | None:
    """Normalize cloud cover percent to fraction if needed."""

    if cloud_cover is None:
        return None
    if cloud_cover > 1.0:
        return cloud_cover / 100.0
    return cloud_cover


def _parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _is_remote_href(href: str) -> bool:
    """Check if href is a remote HTTP(S) URL."""
    return href.startswith(("http://", "https://"))


_WAF_INDICATORS: Final[tuple[str, ...]] = (
    "request rejected",
    "web application firewall",
    "access denied",
    "forbidden",
    "your support id",
    "the requested url was rejected",
)


def _looks_like_waf_response(text: str) -> bool:
    """Detect if response text looks like a WAF rejection page.

    Checks for common WAF indicators in the response body using
    case-insensitive substring matching.

    Args:
        text: Raw response body text to inspect.

    Returns:
        True if the response appears to be a WAF block page.
    """
    text_lower = text.lower()
    return any(indicator in text_lower for indicator in _WAF_INDICATORS)


def _extract_waf_support_id(text: str) -> str | None:
    """Extract a support ID from a WAF response if present.

    Looks for patterns like "Your support ID is: 12345" or
    "Support ID: 12345".

    Args:
        text: Raw WAF response body text.

    Returns:
        The extracted support ID, or None if not found.
    """
    import re

    match = re.search(r"[Ss]upport\s*[Ii][Dd]\s*(?:is:\s*)?(\d+)", text)
    return match.group(1) if match else None


@contextmanager
def _local_asset_context(
    red_href: str, nir_href: str, http_client: httpx.Client | None = None
) -> Iterator[tuple[str, str]]:
    """Context manager that yields local file paths for remote assets.

    Downloads remote JPEG2000/COG assets to temp files to avoid
    OpenJPEG streaming decode errors (opj_get_decoded_tile failed).
    Local file paths are returned unchanged.
    """
    if not _is_remote_href(red_href) and not _is_remote_href(nir_href):
        # Both are local files
        yield red_href, nir_href
        return

    # Create a temporary directory for this request
    with tempfile.TemporaryDirectory(prefix="ndvi_stac_") as tmpdir:
        local_paths = []
        try:
            client = http_client or httpx.Client(
                timeout=60.0,
                follow_redirects=True,
                limits=httpx.Limits(max_connections=10),
            )
            should_close_client = http_client is None

            for href in (red_href, nir_href):
                if _is_remote_href(href):
                    filename = os.path.basename(href.split("?")[0])
                    local_path = os.path.join(tmpdir, filename)
                    resp = client.get(href)
                    resp.raise_for_status()
                    with open(local_path, "wb") as f:
                        f.write(resp.content)
                    local_paths.append(local_path)
                else:
                    local_paths.append(href)

            yield local_paths[0], local_paths[1]
        finally:
            if should_close_client and http_client:
                http_client.close()


def load_ndvi_array(
    *,
    red_href: str,
    nir_href: str,
    bbox: BBox,
    size: int | None,
    timeout_seconds: float,
) -> np.ndarray:
    """Load NDVI values for the given bbox.

    Downloads remote assets to temp files first to avoid
    OpenJPEG streaming decode errors, then processes locally.
    Returns NaN for invalid pixels.
    """

    (
        rasterio,
        resampling_enum,
        rasterio_error,
        transform_bounds,
        from_bounds,
    ) = _require_rasterio()
    gdal_env = {
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "GDAL_HTTP_TIMEOUT": int(timeout_seconds),
        "GDAL_HTTP_CONNECTTIMEOUT": int(timeout_seconds),
        "GDAL_HTTP_MAX_RETRY": 5,
        "GDAL_HTTP_RETRY_DELAY": 2,
        "CPL_CURL_VERBOSE": False,
        "GDAL_NUM_THREADS": "ALL_CPUS",
        "GDAL_CACHEMAX": 256,
    }
    try:
        with _local_asset_context(red_href, nir_href) as (
            local_red,
            local_nir,
        ):
            with rasterio.Env(**gdal_env):
                with (
                    rasterio.open(local_red) as red_ds,
                    rasterio.open(local_nir) as nir_ds,
                ):
                    if red_ds.crs is None or nir_ds.crs is None:
                        raise StacProcessingError(
                            "Raster assets missing CRS metadata."
                        )
                    bounds = transform_bounds(
                        "EPSG:4326",
                        red_ds.crs,
                        float(bbox.west),
                        float(bbox.south),
                        float(bbox.east),
                        float(bbox.north),
                        densify_pts=21,
                    )
                    window = from_bounds(
                        *bounds,
                        transform=red_ds.transform,
                    )
                    out_shape: tuple[int, int] | None = None
                    if size:
                        out_shape = (size, size)
                    resampling = resampling_enum.bilinear
                    red = red_ds.read(
                        1,
                        window=window,
                        out_shape=out_shape,
                        resampling=resampling,
                        masked=True,
                    )
                    nir = nir_ds.read(
                        1,
                        window=window,
                        out_shape=out_shape,
                        resampling=resampling,
                        masked=True,
                    )
    except rasterio_error as exc:
        raise StacProcessingError(f"Raster processing failed: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise StacProcessingError(f"Raster processing failed: {exc}") from exc

    red_data = red.filled(np.nan).astype(np.float32)
    nir_data = nir.filled(np.nan).astype(np.float32)
    logger.info("RED shape=%s", red_data.shape)
    logger.info("NIR shape=%s", nir_data.shape)
    logger.info("RED sample row=%s", np.round(red_data[0, :10], 6))
    logger.info("NIR sample row=%s", np.round(nir_data[0, :10], 6))
    _validate_band_variation(nir_data, "NIR")
    _validate_band_variation(red_data, "RED")
    logger.info(
        "Bands | NIR min=%s max=%s | RED min=%s max=%s",
        float(np.nanmin(nir_data)),
        float(np.nanmax(nir_data)),
        float(np.nanmin(red_data)),
        float(np.nanmax(red_data)),
    )
    denom = nir_data + red_data
    with np.errstate(divide="ignore", invalid="ignore"):
        ndvi = np.where(
            denom != 0,
            (nir_data - red_data) / denom,
            np.nan,
        )
    return ndvi.astype(np.float32)


def _validate_band_variation(data: np.ndarray, band_name: str) -> None:
    """Ensure each band has spatial variation."""

    min_val = float(np.nanmin(data))
    max_val = float(np.nanmax(data))
    if np.isclose(min_val, max_val):
        raise StacProcessingError(f"Invalid {band_name} band: constant raster")


@lru_cache(maxsize=1)
def _require_rasterio() -> tuple[
    Any,
    Any,
    type[Exception],
    Any,
    Any,
]:
    try:
        rasterio = importlib.import_module("rasterio")
        resampling_enum = importlib.import_module("rasterio.enums").Resampling
        rasterio_error = importlib.import_module(
            "rasterio.errors"
        ).RasterioError
        transform_bounds = importlib.import_module(
            "rasterio.warp"
        ).transform_bounds
        from_bounds = importlib.import_module("rasterio.windows").from_bounds
    except ModuleNotFoundError as exc:
        raise StacDependencyError(
            "Rasterio is required for STAC raster processing. "
            "Install rasterio or install the stac extra."
        ) from exc
    return (
        rasterio,
        resampling_enum,
        rasterio_error,
        transform_bounds,
        from_bounds,
    )


def compute_ndvi_stats(ndvi: np.ndarray) -> NdviStats | None:
    """Compute NDVI stats from an NDVI array."""

    if ndvi.size == 0:
        return None
    if np.isnan(ndvi).all():
        return None
    mean = float(np.nanmean(ndvi))
    min_val = float(np.nanmin(ndvi))
    max_val = float(np.nanmax(ndvi))
    sample_count = int(np.count_nonzero(~np.isnan(ndvi)))
    return NdviStats(
        mean=mean,
        min=min_val,
        max=max_val,
        sample_count=sample_count,
    )


class StacClient:
    """Minimal STAC API client for searching items."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        collection: str | None = None,
        timeout_seconds: float | None = None,
        max_items: int | None = None,
    ) -> None:
        api_url_raw = base_url or getattr(
            settings,
            "NDVI_STAC_API_URL",
            DEFAULT_STAC_API_URL,
        )
        api_url = str(api_url_raw).strip()
        if not api_url:
            raise ValueError("NDVI_STAC_API_URL is required")
        self.base_url = api_url.rstrip("/") + "/"
        self.search_url = urljoin(self.base_url, "search")
        self.collection = collection or getattr(
            settings, "NDVI_STAC_COLLECTION", ""
        )
        if not self.collection:
            raise ValueError("NDVI_STAC_COLLECTION is required")
        self.timeout_seconds = timeout_seconds or float(
            getattr(settings, "NDVI_STAC_TIMEOUT_SECS", 30)
        )
        self.max_items = max_items or DEFAULT_MAX_ITEMS

        # Proxy configuration (optional, useful to bypass IP bans)
        proxy_url: str | None = getattr(settings, "NDVI_STAC_PROXY_URL", None)

        self._http = httpx.Client(
            timeout=self.timeout_seconds,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Weather-NDVI-Analyzer/1.0; "
                    "+https://github.com) httpx/" + httpx.__version__
                ),
                "Accept": "application/json, application/geo+json",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
            },
            proxy=proxy_url,
        )

        # Rate limiting configuration
        self._request_interval = float(
            getattr(settings, "NDVI_STAC_REQUEST_INTERVAL_SECS", 10.0)
        )
        self._jitter_min = float(
            getattr(settings, "NDVI_STAC_JITTER_MIN_SECS", 1.0)
        )
        self._jitter_max = float(
            getattr(settings, "NDVI_STAC_JITTER_MAX_SECS", 5.0)
        )
        self._last_request_time = 0.0

        # Circuit breaker configuration
        cb_threshold = int(
            getattr(settings, "NDVI_STAC_CIRCUIT_BREAKER_THRESHOLD", 3)
        )
        cb_timeout = float(
            getattr(settings, "NDVI_STAC_CIRCUIT_BREAKER_TIMEOUT_SECS", 300.0)
        )
        self._circuit_breaker = _CircuitBreaker(
            failure_threshold=cb_threshold,
            reset_timeout_secs=cb_timeout,
        )

    def _apply_throttle(self) -> None:
        """Apply rate limiting with jitter to avoid WAF blocks.

        Waits if the time since the last request is less than the configured
        interval, then adds random jitter to prevent pattern detection.

        Configuration (via Django settings):
            NDVI_STAC_REQUEST_INTERVAL_SECS: Min secs between requests
                (default: 10)
            NDVI_STAC_JITTER_MIN_SECS: Min jitter in seconds (default: 1)
            NDVI_STAC_JITTER_MAX_SECS: Max jitter in seconds (default: 5)
        """
        now = time.monotonic()
        elapsed = now - self._last_request_time

        # Wait if we're requesting too fast
        if elapsed < self._request_interval:
            wait_time = self._request_interval - elapsed
            logger.debug(
                "STAC throttling: waiting %.2fs before next request",
                wait_time,
            )
            time.sleep(wait_time)

        # Add jitter to prevent pattern detection
        # Not for cryptographic use, just to avoid WAF pattern detection
        jitter = random.uniform(self._jitter_min, self._jitter_max)  # noqa: S311
        logger.debug(
            "STAC request: adding %.2fs jitter (range: %.1f-%.1fs)",
            jitter,
            self._jitter_min,
            self._jitter_max,
        )
        time.sleep(jitter)

        self._last_request_time = time.monotonic()

    def search(
        self,
        *,
        bbox: BBox | tuple[float, float, float, float] | list[float],
        start: date,
        end: date,
        max_cloud: int,
        farm_id: int | None = None,
        job_id: int | None = None,
    ) -> list[StacItem]:
        """Search STAC for items within a date range and bbox."""

        normalized_bbox = normalize_stac_bbox(
            bbox,
            farm_id=farm_id,
            job_id=job_id,
        )
        payload: dict[str, Any] = {
            "collections": [self.collection],
            "bbox": list(normalized_bbox),
            "datetime": (
                f"{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59Z"
            ),
            "limit": min(DEFAULT_LIMIT, self.max_items),
        }
        items: list[StacItem] = []
        next_url: str | None = self.search_url
        next_method = "POST"
        next_payload: dict[str, Any] | None = payload

        while next_url and len(items) < self.max_items:
            # Check circuit breaker before each request
            if self._circuit_breaker.is_open():
                raise StacUpstreamError(
                    "STAC API request blocked: circuit breaker is open. "
                    "The upstream service appears to be unreachable. "
                    "Will retry after the circuit breaker timeout expires.",
                    retryable=False,
                )

            # Apply rate limiting with jitter before each request
            self._apply_throttle()

            response = self._request(
                next_method,
                next_url,
                json=next_payload,
            )
            data = self._parse_json_response(response)
            self._circuit_breaker.record_success()
            parsed = self._parse_items(data)
            items.extend(filter_items_by_cloud(parsed, max_cloud))
            next_url, next_method, next_payload = self._next_link(data)

        return items[: self.max_items]

    def _request(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            response = self._http.request(method, url, json=json)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            self._circuit_breaker.record_failure()
            status_code = exc.response.status_code if exc.response else None
            snippet = self._response_snippet(exc.response)
            retryable = bool(status_code and status_code >= 500)
            message = f"STAC request failed status={status_code}"
            if snippet:
                message = f"{message} body={snippet}"
            raise StacUpstreamError(
                message,
                status_code=status_code,
                retryable=retryable,
            ) from exc
        except httpx.RequestError as exc:
            self._circuit_breaker.record_failure()
            raise StacUpstreamError(
                f"STAC request failed: {exc}",
                retryable=True,
            ) from exc

    def _response_snippet(self, response: httpx.Response | None) -> str | None:
        if response is None:
            return None
        try:
            text = response.text.strip()
        except Exception:
            return None
        if not text:
            return None
        normalized = " ".join(text.splitlines())
        if len(normalized) > MAX_ERROR_SNIPPET_CHARS:
            normalized = f"{normalized[:MAX_ERROR_SNIPPET_CHARS]}..."
        return normalized

    def _parse_json_response(
        self,
        response: httpx.Response,
    ) -> dict[str, Any]:
        """Parse JSON response with graceful handling for empty bodies.

        Args:
            response: HTTP response from STAC API.

        Returns:
            Parsed JSON as dict.

        Raises:
            StacWafBlockedError: If response is a WAF block page
                (non-retryable).
            StacUpstreamError: If response is empty or invalid JSON.
        """
        content = response.content
        if not content or len(content.strip()) == 0:
            raise StacUpstreamError(
                f"STAC API returned empty response body "
                f"(status={response.status_code})",
                retryable=True,
            )

        # Detect WAF block early before attempting JSON parse
        headers = getattr(response, "headers", {})
        content_type = headers.get("content-type", "").lower()
        response_text = getattr(response, "text", "")
        if "html" in content_type or "<html" in response_text[:100].lower():
            if _looks_like_waf_response(response_text):
                self._circuit_breaker.record_failure()
                support_id = _extract_waf_support_id(response_text)
                message = (
                    f"STAC API blocked by WAF (status={response.status_code}"
                )
                if support_id:
                    message = f"{message}, support_id={support_id}"
                message = f"{message})"
                raise StacWafBlockedError(
                    message,
                    support_id=support_id,
                )

        try:
            return response.json()
        except ValueError as exc:
            self._circuit_breaker.record_failure()
            snippet = self._response_snippet(response)
            body = snippet or "<empty>"
            raise StacUpstreamError(
                f"STAC API returned invalid JSON "
                f"(status={response.status_code}, body={body})",
                retryable=True,
            ) from exc

    def _parse_items(self, data: dict[str, Any]) -> list[StacItem]:
        items: list[StacItem] = []
        features = data.get("features")
        if not isinstance(features, list):
            return items
        for feature in features:
            if not isinstance(feature, dict):
                continue
            item_id = str(feature.get("id") or "")
            collection_raw = feature.get("collection")
            collection = str(collection_raw) if collection_raw else None
            properties = feature.get("properties") or {}
            raw_dt = (
                properties.get("datetime")
                or properties.get("start_datetime")
                or properties.get("end_datetime")
            )
            if not raw_dt:
                continue
            dt = _parse_datetime(raw_dt)
            if dt is None:
                continue
            cloud_val = properties.get("eo:cloud_cover")
            if cloud_val is None:
                cloud_val = properties.get("cloud_cover")
            cloud_cover: float | None = None
            if cloud_val is not None:
                try:
                    cloud_cover = float(cloud_val)
                except (TypeError, ValueError):
                    cloud_cover = None
            assets = self._parse_assets(feature)
            items.append(
                StacItem(
                    id=item_id,
                    datetime=dt,
                    assets=assets,
                    cloud_cover=cloud_cover,
                    collection=collection,
                )
            )
        return items

    def _parse_assets(self, feature: dict[str, Any]) -> dict[str, str]:
        assets = feature.get("assets")
        if not isinstance(assets, dict):
            return {}
        links = feature.get("links")
        base_href: str | None = None
        if isinstance(links, list):
            for link in links:
                if not isinstance(link, dict):
                    continue
                if link.get("rel") == "self" and link.get("href"):
                    base_href = str(link["href"])
                    break
        asset_map: dict[str, str] = {}
        for name, asset in assets.items():
            if not isinstance(asset, dict):
                continue
            href = asset.get("href")
            if not href:
                continue
            href_str = str(href)
            if not href_str.startswith("http"):
                href_str = urljoin(base_href or self.base_url, href_str)
            asset_map[str(name)] = href_str
        return asset_map

    def _next_link(
        self, data: dict[str, Any]
    ) -> tuple[str | None, str, dict[str, Any] | None]:
        links = data.get("links")
        if not isinstance(links, list):
            return None, "GET", None
        for link in links:
            if not isinstance(link, dict):
                continue
            if link.get("rel") != "next":
                continue
            href = link.get("href")
            if not href:
                continue
            method = str(link.get("method") or "GET").upper()
            return str(href), method, None
        return None, "GET", None
