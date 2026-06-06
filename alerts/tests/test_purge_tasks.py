"""Tests for the alerts and radio retention purge tasks.

Per ``prompts/p4-staff-engineer-review.md`` #2 the audio-alerts,
listening-history, and health-check tables grow unbounded. The
purge tasks are scheduled daily by Celery Beat and obey
environment-driven retention windows. These tests pin the
contract: rows older than the window are deleted, files are
removed from storage, and the opt-out (retention=0) switch
short-circuits.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.utils import timezone

from alerts.models import AudioAlert, AudioAlertTriggerSource, AudioAlertType
from alerts.tasks import purge_old_alerts, purge_orphan_audio_files
from radio.models import (
    ListeningHistory,
    Provider,
    Station,
    StationHealthCheck,
)
from radio.tasks import purge_old_health_checks, purge_old_history

User = get_user_model()


def _make_user() -> Any:
    return User.objects.create_user(
        username=f"u-{secrets.token_urlsafe(8)}",
        password=secrets.token_urlsafe(16),
    )


def _make_alert(user: Any, *, days_ago: int) -> AudioAlert:
    alert = AudioAlert.objects.create(
        user=user,
        alert_type=AudioAlertType.ADMIN_BROADCAST,
        trigger_source=AudioAlertTriggerSource.ADMIN_VIEW,
        title="t",
        message="m",
    )
    if days_ago:
        alert.created_at = timezone.now() - timedelta(days=days_ago)
        alert.save(update_fields=["created_at"])
    return alert


# --- alerts.tasks.purge_old_alerts ----------------------------------------


class PurgeOldAlertsTests(TestCase):
    def test_deletes_rows_older_than_window(self) -> None:
        user = _make_user()
        old = _make_alert(user, days_ago=100)
        recent = _make_alert(user, days_ago=10)
        with override_settings(ALERTS_RETENTION_DAYS=90):
            result = purge_old_alerts.run()
        assert result["deleted"] == 1
        assert result["retention_days"] == 90
        assert not AudioAlert.objects.filter(id=old.id).exists()
        assert AudioAlert.objects.filter(id=recent.id).exists()

    def test_retention_zero_short_circuits(self) -> None:
        user = _make_user()
        old = _make_alert(user, days_ago=1000)
        with override_settings(ALERTS_RETENTION_DAYS=0):
            result = purge_old_alerts.run()
        assert result["deleted"] == 0
        assert result["retention_days"] == 0
        assert AudioAlert.objects.filter(id=old.id).exists()

    def test_audio_file_removed_before_row_delete(self) -> None:
        user = _make_user()
        old = _make_alert(user, days_ago=200)
        old.audio_file.save(
            "audio_alerts/test.wav",
            ContentFile(b"RIFF"),
            save=True,
        )
        stored_name = old.audio_file.name
        assert old.audio_file.storage.exists(stored_name)
        with override_settings(ALERTS_RETENTION_DAYS=90):
            purge_old_alerts.run()
        assert not old.audio_file.storage.exists(stored_name)
        assert not AudioAlert.objects.filter(id=old.id).exists()


# --- alerts.tasks.purge_orphan_audio_files --------------------------------


class PurgeOrphanAudioFilesTests(TestCase):
    def setUp(self) -> None:
        """Wipe ``MEDIA_ROOT/audio_alerts/`` so each test
        starts from a clean directory tree. The test backend
        resets the DB between tests but it does not touch the
        file system, so files saved by an earlier test would
        otherwise bleed into the next one.
        """
        from django.core.files.storage import default_storage

        try:
            dirs, files = default_storage.listdir("audio_alerts/")
        except FileNotFoundError:
            return
        for f in files:
            default_storage.delete(f"audio_alerts/{f}")
        for sub in dirs:
            self._wipe_recursive(f"audio_alerts/{sub}/")

    def _wipe_recursive(self, directory: str) -> None:
        from django.core.files.storage import default_storage

        try:
            sub_dirs, files = default_storage.listdir(directory)
        except FileNotFoundError:
            return
        for f in files:
            default_storage.delete(f"{directory}{f}")
        for sub in sub_dirs:
            self._wipe_recursive(f"{directory}{sub}/")

    def test_removes_files_without_live_row(self) -> None:
        # Create an alert with an audio file, then delete the row
        # without touching the file (simulating a manual cleanup).
        from django.core.files.storage import default_storage

        path = "audio_alerts/orphan.wav"
        default_storage.save(path, ContentFile(b"RIFF"))
        assert default_storage.exists(path)
        with patch(
            "alerts.tasks.AudioAlert.objects.filter",
            return_value=type("Q", (), {"exists": lambda self: False})(),
        ):
            result = purge_orphan_audio_files.run()
        assert result["removed"] == 1
        assert not default_storage.exists(path)

    def test_keeps_files_referenced_by_live_row(self) -> None:
        from django.core.files.storage import default_storage

        user = _make_user()
        alert = _make_alert(user, days_ago=0)
        alert.audio_file.save(
            "audio_alerts/keep.wav",
            ContentFile(b"RIFF"),
            save=True,
        )
        path = alert.audio_file.name
        assert default_storage.exists(path)
        result = purge_orphan_audio_files.run()
        assert result["removed"] == 0
        assert default_storage.exists(path)

    def test_walks_year_month_day_subdirs(self) -> None:
        """The orphan-file walk must descend into the
        ``audio_alerts/YYYY/MM/DD/`` tree that ``FileField``
        produces (see ``upload_to`` on
        ``AudioAlert.audio_file``) and remove any file with
        no live row pointing at it.
        """
        from django.core.files.storage import default_storage

        path = "audio_alerts/2026/06/05/orphan.wav"
        default_storage.save(path, ContentFile(b"RIFF"))
        assert default_storage.exists(path)
        with patch(
            "alerts.tasks.AudioAlert.objects.filter",
            return_value=type("Q", (), {"exists": lambda self: False})(),
        ):
            result = purge_orphan_audio_files.run()
        assert result["removed"] == 1
        assert not default_storage.exists(path)

    def test_returns_zero_when_storage_dir_missing(self) -> None:
        """If ``MEDIA_ROOT/audio_alerts/`` does not exist yet
        (fresh install), the purge task short-circuits with
        ``removed=0`` instead of raising.
        """
        from unittest.mock import MagicMock

        fake_storage = MagicMock()
        fake_storage.listdir.side_effect = FileNotFoundError("no dir")
        # The task imports default_storage lazily from
        # ``django.core.files.storage``, so we have to patch
        # the source module rather than ``alerts.tasks``.
        with patch("django.core.files.storage.default_storage", fake_storage):
            result = purge_orphan_audio_files.run()
        assert result == {"removed": 0}


# --- radio.tasks.purge_old_history ----------------------------------------


def _make_provider() -> Provider:
    return Provider.objects.create(
        slug=f"p-{secrets.token_urlsafe(6)}",
        name=f"p-{secrets.token_urlsafe(6)}",
        provider_type="aggregator",
        is_active=True,
    )


def _make_station(provider: Provider) -> Station:
    return Station.objects.create(
        id=f"s-{secrets.token_urlsafe(6)}",
        name="s",
        provider=provider,
        country="UK",
        language="English",
        stream_url="https://example.test/s",
        is_active=True,
    )


class PurgeOldHistoryTests(TestCase):
    def test_deletes_rows_older_than_window(self) -> None:
        user = _make_user()
        provider = _make_provider()
        station = _make_station(provider)
        old = ListeningHistory.objects.create(user=user, station=station)
        old.started_at = timezone.now() - timedelta(days=100)
        old.save(update_fields=["started_at"])
        recent = ListeningHistory.objects.create(user=user, station=station)
        with override_settings(RADIO_HISTORY_RETENTION_DAYS=90):
            result = purge_old_history.run()
        assert result["deleted"] == 1
        assert result["retention_days"] == 90
        assert not ListeningHistory.objects.filter(id=old.id).exists()
        assert ListeningHistory.objects.filter(id=recent.id).exists()

    def test_retention_zero_short_circuits(self) -> None:
        user = _make_user()
        provider = _make_provider()
        station = _make_station(provider)
        old = ListeningHistory.objects.create(user=user, station=station)
        old.started_at = timezone.now() - timedelta(days=1000)
        old.save(update_fields=["started_at"])
        with override_settings(RADIO_HISTORY_RETENTION_DAYS=0):
            result = purge_old_history.run()
        assert result["deleted"] == 0
        assert ListeningHistory.objects.filter(id=old.id).exists()


# --- radio.tasks.purge_old_health_checks ----------------------------------


class PurgeOldHealthChecksTests(TestCase):
    def test_keeps_only_n_newest_per_station(self) -> None:
        provider = _make_provider()
        station = _make_station(provider)
        now = timezone.now()
        # 5 health checks; keep 2 newest.
        for i in range(5):
            row = StationHealthCheck.objects.create(
                station=station, is_reachable=True
            )
            row.checked_at = now - timedelta(hours=5 - i)
            row.save(update_fields=["checked_at"])
        with override_settings(RADIO_HEALTH_CHECK_KEEP_PER_STATION=2):
            result = purge_old_health_checks.run()
        assert result["deleted"] == 3
        assert result["keep_per_station"] == 2
        remaining = StationHealthCheck.objects.filter(
            station=station
        ).order_by("-checked_at")
        assert remaining.count() == 2
        # The two newest are kept.
        assert remaining[0].checked_at >= remaining[1].checked_at

    def test_keep_zero_short_circuits(self) -> None:
        provider = _make_provider()
        station = _make_station(provider)
        StationHealthCheck.objects.create(station=station, is_reachable=True)
        with override_settings(RADIO_HEALTH_CHECK_KEEP_PER_STATION=0):
            result = purge_old_health_checks.run()
        assert result["deleted"] == 0
        assert StationHealthCheck.objects.filter(station=station).count() == 1

    def test_handles_station_with_fewer_rows_than_keep(self) -> None:
        provider = _make_provider()
        station = _make_station(provider)
        StationHealthCheck.objects.create(station=station, is_reachable=True)
        with override_settings(RADIO_HEALTH_CHECK_KEEP_PER_STATION=10):
            result = purge_old_health_checks.run()
        assert result["deleted"] == 0
        assert StationHealthCheck.objects.filter(station=station).count() == 1
