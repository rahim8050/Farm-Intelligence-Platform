#!/usr/bin/env python3
"""Celery Prometheus metrics exporter — standalone (no Django)."""

import argparse
import logging
import time

import prometheus_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# --- Prometheus metrics ---
queue_length = prometheus_client.Gauge(
    "celery_queue_length",
    "Number of tasks in queue",
    ["queue"],
)

active_tasks = prometheus_client.Gauge("celery_active_tasks", "Active tasks")
worker_count = prometheus_client.Gauge(
    "celery_worker_count", "Number of workers"
)
reserved_tasks = prometheus_client.Gauge(
    "celery_reserved_tasks", "Reserved tasks"
)
scheduled_tasks = prometheus_client.Gauge(
    "celery_scheduled_tasks", "Scheduled tasks"
)


def collect_metrics(redis_url: str) -> None:
    from redis import Redis

    r = Redis.from_url(redis_url)

    known_queues = [
        "celery",
        "default",
        "ndvi_ingestion",
        "ndvi_recompute",
        "ndvi_analysis",
        "ingestion",
        "quality",
        "fusion",
        "raster",
    ]
    for q in known_queues:
        try:
            queue_length.labels(queue=q).set(r.llen(q))  # type: ignore[arg-type]
        except Exception as exc:
            logger.debug("queue %s: %s", q, exc)

    # Get celery worker info via Redis keys
    try:
        workers: list = r.keys("celery-worker*")  # type: ignore[assignment]
        worker_count.set(len(workers))
    except Exception as exc:
        logger.debug("worker_count: %s", exc)

    # Active/reserved/scheduled via stats
    try:
        active: list = r.keys("*active*")  # type: ignore[assignment]
        active_tasks.set(len(active))
    except Exception as exc:
        logger.debug("active_tasks: %s", exc)

    r.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8003)
    parser.add_argument("--redis", default="redis://127.0.0.1:16379/0")
    args = parser.parse_args()

    prometheus_client.start_http_server(args.port)
    logger.info("Celery metrics exporter started on :%d/metrics", args.port)
    logger.info("Redis: %s", args.redis)

    while True:
        try:
            collect_metrics(args.redis)
        except Exception as exc:
            logger.error("collect_metrics failed: %s", exc)
        time.sleep(15)


if __name__ == "__main__":
    main()
