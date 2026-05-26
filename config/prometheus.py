from __future__ import annotations

import logging
import os
from pathlib import Path

from prometheus_client.mmap_dict import MmapedDict

logger = logging.getLogger(__name__)


def prometheus_multiprocess_dir() -> Path | None:
    """Return the configured Prometheus multiprocess directory, if any."""
    raw_dir = os.environ.get("PROMETHEUS_MULTIPROC_DIR") or os.environ.get(
        "prometheus_multiproc_dir"
    )
    if not raw_dir:
        return None
    return Path(raw_dir)


def clear_prometheus_multiprocess_dir() -> int:
    """Remove all files from the Prometheus multiprocess directory."""
    metrics_dir = prometheus_multiprocess_dir()
    if metrics_dir is None or not metrics_dir.exists():
        return 0

    removed = 0
    for path in metrics_dir.iterdir():
        if not path.is_file():
            continue
        try:
            path.unlink()
        except OSError as exc:
            logger.warning(
                "Unable to remove stale Prometheus shard %s: %s",
                path,
                exc,
            )
        else:
            removed += 1
    return removed


def sanitize_prometheus_multiprocess_dir() -> int:
    """Clear the multiprocess directory if any shard file is corrupted."""
    metrics_dir = prometheus_multiprocess_dir()
    if metrics_dir is None or not metrics_dir.exists():
        return 0

    for path in metrics_dir.iterdir():
        if not path.is_file():
            continue
        try:
            probe = MmapedDict(str(path))
        except Exception as exc:
            removed = clear_prometheus_multiprocess_dir()
            logger.warning(
                "Prometheus multiprocess metrics were corrupted; "
                "cleared %s shard files (%s)",
                removed,
                exc,
            )
            return removed
        else:
            probe.close()

    return 0
