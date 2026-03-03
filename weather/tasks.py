"""Celery tasks for weather app maintenance and warmups."""

from __future__ import annotations

import logging

from celery import shared_task

from config.views import warm_openapi_schema_cache_variants

logger = logging.getLogger(__name__)


@shared_task
def warm_openapi_schema_cache() -> int:
    """Warm cached OpenAPI schema variants for low-latency schema reads."""

    warmed = warm_openapi_schema_cache_variants()
    logger.info("schema.cache.warmed variants=%s", warmed)
    return warmed
