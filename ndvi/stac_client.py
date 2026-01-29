"""STAC client utilities for NDVI item selection and raster processing."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final
from urllib.parse import urljoin

import httpx
import numpy as np
import rasterio
from django.conf import settings
from rasterio.enums import Resampling
from rasterio.errors import RasterioError
from rasterio.warp import transform_bounds
from rasterio.windows import from_bounds

from ndvi.engines.base import BBox

MAX_ERROR_SNIPPET_CHARS = 1600
DEFAULT_MAX_ITEMS = 500
DEFAULT_LIMIT = 200
DEFAULT_STATS_SAMPLE_SIZE = 128
DEFAULT_STAC_API_URL: Final[str] = "https://stac.dataspace.copernicus.eu/v1/"


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


class StacProcessingError(StacError):
    """Raised when raster processing fails."""


@dataclass(frozen=True)
class StacItem:
    """Parsed STAC item metadata needed for NDVI extraction."""

    id: str
    datetime: datetime
    assets: dict[str, str]
    cloud_cover: float | None

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


def resolve_asset_href(item: StacItem, asset_name: str) -> str | None:
    """Resolve an asset href by name (case-insensitive)."""

    if asset_name in item.assets:
        return item.assets[asset_name]
    lowered = asset_name.lower()
    for key, href in item.assets.items():
        if key.lower() == lowered:
            return href
    return None


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


def load_ndvi_array(
    *,
    red_href: str,
    nir_href: str,
    bbox: BBox,
    size: int | None,
    timeout_seconds: float,
) -> np.ndarray:
    """Load NDVI values for the given bbox.

    Returns NaN for invalid pixels.
    """

    gdal_env = {
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "GDAL_HTTP_TIMEOUT": str(int(timeout_seconds)),
        "GDAL_HTTP_CONNECTTIMEOUT": str(int(timeout_seconds)),
        "GDAL_HTTP_MAX_RETRY": "0",
    }
    try:
        with rasterio.Env(**gdal_env):
            with (
                rasterio.open(red_href) as red_ds,
                rasterio.open(nir_href) as nir_ds,
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
                resampling = Resampling.bilinear
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
    except RasterioError as exc:
        raise StacProcessingError(f"Raster processing failed: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise StacProcessingError(f"Raster processing failed: {exc}") from exc

    red_data = red.filled(np.nan).astype(np.float32)
    nir_data = nir.filled(np.nan).astype(np.float32)
    denom = nir_data + red_data
    with np.errstate(divide="ignore", invalid="ignore"):
        ndvi = np.where(
            denom != 0,
            (nir_data - red_data) / denom,
            np.nan,
        )
    return ndvi.astype(np.float32)


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
        self._http = httpx.Client(timeout=self.timeout_seconds)

    def search(
        self,
        *,
        bbox: BBox,
        start: date,
        end: date,
        max_cloud: int,
    ) -> list[StacItem]:
        """Search STAC for items within a date range and bbox."""

        payload: dict[str, Any] = {
            "collections": [self.collection],
            "bbox": [
                float(bbox.west),
                float(bbox.south),
                float(bbox.east),
                float(bbox.north),
            ],
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
            response = self._request(
                next_method,
                next_url,
                json=next_payload,
            )
            data = response.json()
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

    def _parse_items(self, data: dict[str, Any]) -> list[StacItem]:
        items: list[StacItem] = []
        features = data.get("features")
        if not isinstance(features, list):
            return items
        for feature in features:
            if not isinstance(feature, dict):
                continue
            item_id = str(feature.get("id") or "")
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
