from __future__ import annotations

import errno
from pathlib import Path
from unittest.mock import patch

import pytest

from config.prometheus import (
    clear_prometheus_multiprocess_dir,
    prometheus_multiprocess_dir,
    sanitize_prometheus_multiprocess_dir,
)


def test_prometheus_multiprocess_dir_returns_none_when_not_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
    monkeypatch.delenv("prometheus_multiproc_dir", raising=False)
    assert prometheus_multiprocess_dir() is None


def test_prometheus_multiprocess_dir_returns_path_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = tmp_path / "prom"
    d.mkdir()
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(d))
    result = prometheus_multiprocess_dir()
    assert result == d


def test_prometheus_multiprocess_dir_uses_fallback_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = tmp_path / "metrics"
    d.mkdir()
    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
    monkeypatch.setenv("prometheus_multiproc_dir", str(d))
    result = prometheus_multiprocess_dir()
    assert result == d


def test_clear_prometheus_multiprocess_dir_returns_0_when_no_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
    monkeypatch.delenv("prometheus_multiproc_dir", raising=False)
    assert clear_prometheus_multiprocess_dir() == 0


def test_clear_prometheus_multiprocess_dir_returns_0_for_nonexistent_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", "/nonexistent")
    assert clear_prometheus_multiprocess_dir() == 0


def test_clear_removes_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = tmp_path / "prom"
    d.mkdir()
    (d / "shard_1.db").write_text("data", encoding="utf-8")
    (d / "shard_2.db").write_text("data", encoding="utf-8")
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(d))
    assert clear_prometheus_multiprocess_dir() == 2
    assert not (d / "shard_1.db").exists()
    assert not (d / "shard_2.db").exists()


def test_clear_skips_subdirectories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = tmp_path / "prom"
    d.mkdir()
    sub = d / "sub"
    sub.mkdir()
    (d / "shard.db").write_text("data", encoding="utf-8")
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(d))
    assert clear_prometheus_multiprocess_dir() == 1
    assert sub.exists()


def test_clear_logs_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    d = tmp_path / "prom"
    d.mkdir()
    shard = d / "shard.db"
    shard.write_text("data", encoding="utf-8")
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(d))

    orig_unlink = shard.unlink
    fail_count = 0

    def failing_unlink(*args: object, **kwargs: object) -> None:
        nonlocal fail_count
        fail_count += 1
        if fail_count == 1:
            raise OSError(errno.EACCES, "Permission denied")
        orig_unlink()

    with patch.object(Path, "unlink", failing_unlink):
        assert clear_prometheus_multiprocess_dir() == 0

    assert "Unable to remove stale Prometheus shard" in caplog.text


def test_sanitize_returns_0_when_no_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
    monkeypatch.delenv("prometheus_multiproc_dir", raising=False)
    assert sanitize_prometheus_multiprocess_dir() == 0


def test_sanitize_returns_0_when_no_corruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = tmp_path / "prom"
    d.mkdir()
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(d))
    assert sanitize_prometheus_multiprocess_dir() == 0


def test_sanitize_clears_on_probe_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    d = tmp_path / "prom"
    d.mkdir()
    (d / "shard.db").write_text("corrupted", encoding="utf-8")
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(d))

    result = sanitize_prometheus_multiprocess_dir()

    assert result == 1
    assert "Prometheus multiprocess metrics were corrupted" in caplog.text
    assert not (d / "shard.db").exists()


def test_sanitize_skips_subdirectories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = tmp_path / "prom"
    d.mkdir()
    sub = d / "sub"
    sub.mkdir()
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(d))
    assert sanitize_prometheus_multiprocess_dir() == 0
    assert sub.exists()
