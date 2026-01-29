from __future__ import annotations

from functools import lru_cache

from django.conf import settings
from django.utils.module_loading import import_string
from rest_framework.exceptions import ValidationError

from .base import NdviRasterEngine

DEFAULT_RASTER_ENGINE_NAME = "sentinelhub"
SUPPORTED_RASTER_ENGINES = ("sentinelhub", "stac")
RASTER_ENGINE_PATHS = {
    "sentinelhub": "ndvi.raster.sentinelhub_engine.SentinelHubRasterEngine",
    "stac": "ndvi.raster.stac_compute_engine.StacComputeRasterEngine",
}
RASTER_ENGINE_SETTING_KEYS = {
    "sentinelhub": "NDVI_RASTER_ENGINE_PATH",
    "stac": "NDVI_RASTER_ENGINE_PATH_STAC",
}


def resolve_raster_engine_name(
    engine_name: str | None,
    *,
    default_engine: str | None = None,
) -> str:
    resolved = (
        engine_name
        if engine_name is not None
        else default_engine
        or getattr(
            settings,
            "NDVI_RASTER_ENGINE_NAME",
            getattr(settings, "NDVI_ENGINE", DEFAULT_RASTER_ENGINE_NAME),
        )
    )
    engine = str(resolved).lower()
    if engine not in SUPPORTED_RASTER_ENGINES:
        raise ValidationError(
            "raster engine must be one of: "
            f"{', '.join(SUPPORTED_RASTER_ENGINES)}."
        )
    return engine


def _resolve_engine_path(engine_key: str) -> str:
    setting_key = RASTER_ENGINE_SETTING_KEYS[engine_key]
    default_path = RASTER_ENGINE_PATHS[engine_key]
    return str(getattr(settings, setting_key, default_path))


@lru_cache(maxsize=4)
def _get_engine_by_name(engine_key: str) -> NdviRasterEngine:
    engine_path = _resolve_engine_path(engine_key)
    engine_cls: type[NdviRasterEngine] = import_string(engine_path)
    return engine_cls()  # type: ignore[call-arg]


def get_engine(engine_name: str | None = None) -> NdviRasterEngine:
    """Return the configured raster engine instance."""

    engine_key = resolve_raster_engine_name(engine_name)
    return _get_engine_by_name(engine_key)
