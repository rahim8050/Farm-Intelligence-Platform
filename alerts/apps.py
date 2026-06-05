import logging

from django.apps import AppConfig

logger = logging.getLogger("alerts.apps")


class AlertsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "alerts"
    verbose_name = "Farm audio alerts"

    def ready(self) -> None:
        """Publish the initial TTS circuit-breaker state on startup.

        Per ``prompts/p4-staff-engineer-review.md`` #7 the gauge
        ``alerts_tts_circuit_state{engine}`` must reflect the
        current state at process start so the SLO rule
        ``AlertsTTSCircuitOpen`` can fire immediately. The breaker
        is process-local (it lives in this worker's memory) and
        starts closed, so this is mostly a no-op; but if a future
        Celery worker is restarted with a breaker that we want to
        publish in a half-open state, this hook is the place.
        """
        from .tts_breaker import probe_engines

        try:
            state = probe_engines()
            if state:
                logger.info("TTS circuit-breaker state: %s", state)
        except Exception:  # noqa: BLE001 - never block startup
            logger.warning("TTS circuit-breaker probe failed", exc_info=True)
