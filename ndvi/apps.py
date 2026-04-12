from __future__ import annotations

from django.apps import AppConfig
from django.conf import settings


class NdviConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "ndvi"
    verbose_name = "NDVI"

    def ready(self) -> None:
        """Initialize circuit breakers for all NDVI engines at startup.

        This ensures Prometheus metrics are available immediately,
        even before any NDVI requests have been made.
        """
        from ndvi.circuit_breaker import (
            CircuitBreaker,
            get_circuit_breaker,
            register_circuit_breaker,
        )

        engines = [
            ("stac", "NDVI_STAC_CIRCUIT_BREAKER"),
            ("sentinelhub", "NDVI_SENTINELHUB_CIRCUIT_BREAKER"),
            ("sentinelhub_raster", "NDVI_SENTINELHUB_CIRCUIT_BREAKER"),
        ]

        for engine_name, setting_prefix in engines:
            if get_circuit_breaker(engine_name) is not None:
                continue  # Already registered (e.g., from engine init)

            threshold = int(
                getattr(settings, f"{setting_prefix}_THRESHOLD", 3)
            )
            timeout = float(
                getattr(settings, f"{setting_prefix}_TIMEOUT_SECS", 300.0)
            )
            cb = CircuitBreaker(
                engine=engine_name,
                failure_threshold=threshold,
                reset_timeout_secs=timeout,
            )
            register_circuit_breaker(cb)
