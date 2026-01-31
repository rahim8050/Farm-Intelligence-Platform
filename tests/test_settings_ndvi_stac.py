from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load_settings_module(module_name: str) -> ModuleType:
    settings_path = (
        Path(__file__).resolve().parent.parent / "config" / "settings.py"
    )
    spec = importlib.util.spec_from_file_location(module_name, settings_path)
    if spec is None or spec.loader is None:
        raise AssertionError("Unable to load config.settings for test")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_ndvi_stac_settings_read_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DJANGO_SECRET_KEY", "test-secret")
    monkeypatch.setenv("NDVI_STAC_COLLECTION", "sentinel-2-l2a")
    monkeypatch.setenv("NDVI_STAC_ASSET_RED", "B04_10m")
    monkeypatch.setenv("NDVI_STAC_ASSET_NIR", "B08_10m")

    module_name = "temp_ndvi_stac_settings"
    module = _load_settings_module(module_name)
    try:
        assert module.NDVI_STAC_COLLECTION == "sentinel-2-l2a"
        assert module.NDVI_STAC_ASSET_RED == "B04_10m"
        assert module.NDVI_STAC_ASSET_NIR == "B08_10m"
    finally:
        sys.modules.pop(module_name, None)
