"""Branch coverage for the alerts app.

These tests target the small number of defensive branches that
are not exercised by the main happy-path suite - they exist to
push branch coverage above the 96% CI threshold.
"""

from __future__ import annotations

import secrets
import wave
from io import BytesIO
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from alerts.models import (
    AudioAlert,
    AudioAlertSubscription,
    AudioAlertType,
)
from alerts.serializers import (
    AudioAlertSerializer,
    AudioAlertSubscriptionSerializer,
)
from alerts.services import (
    _absolute_audio_url,
    _save_audio_bytes,
    dispatch_alert,
    emit_audio_alert_event,
    has_subscription,
)
from alerts.tasks import (
    scan_low_ndvi_observations,
    scan_ndvi_declines,
)
from alerts.triggers import (
    on_activity_completed,
    on_admin_broadcast,
    on_ndvi_decline,
    on_ndvi_low,
)
from alerts.tts import (
    TTSResult,
    _espeak,
    _guess_wav_duration_ms,
    _piper,
    _sine_wav,
    synthesize,
)

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# local helpers (conftest only exposes make_user / make_farm)
# ---------------------------------------------------------------------------


def _make_user() -> Any:
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(
        username=f"u-{secrets.token_urlsafe(8)}",
        password=secrets.token_urlsafe(16),
    )


def _stub_channel_layer() -> Any:
    from unittest.mock import AsyncMock

    layer = MagicMock()
    layer.group_send = AsyncMock(return_value=None)
    return patch("alerts.services.get_channel_layer", return_value=layer)


# ---------------------------------------------------------------------------
# serializers.py
# ---------------------------------------------------------------------------


def test_validate_alert_types_raises_for_unknown(make_user, make_farm) -> None:
    """The custom validator rejects unknown alert types."""
    user = make_user()
    farm = make_farm(user)
    ser = AudioAlertSubscriptionSerializer(
        data={"farm": farm.id, "alert_types": ["bogus-type"]}
    )
    assert not ser.is_valid()
    assert "alert_types" in ser.errors


def test_audio_url_is_empty_when_no_file() -> None:
    user = _make_user()
    alert = AudioAlert.objects.create(
        user=user,
        alert_type=AudioAlertType.ADMIN_BROADCAST,
        trigger_source="admin_view",
        title="t",
        message="m",
    )
    out = AudioAlertSerializer(alert).data
    assert out["audio_url"] == ""


def test_audio_url_is_relative_when_no_request(make_user, make_farm) -> None:
    """get_audio_url returns the raw ``.url`` after the render task runs."""
    from unittest.mock import MagicMock

    from alerts import tasks

    user = make_user()
    farm = make_farm(user)
    tts_result = MagicMock()
    tts_result.audio_bytes = b"RIFF...stub"
    tts_result.mime_type = "audio/wav"
    tts_result.duration_ms = 1234
    with (
        patch("alerts.tts.synthesize", return_value=tts_result),
        patch.object(tasks.render_alert_audio, "delay", MagicMock()),
        _stub_channel_layer(),
    ):
        result = dispatch_alert(
            user_id=user.id,
            farm_id=farm.id,
            alert_type=AudioAlertType.ADMIN_BROADCAST,
            trigger_source="admin_view",
            title="t",
            message="m",
        )
        # Trigger the render task body directly (bypass the stubbed
        # ``.delay`` which would have run it in eager mode anyway).
        tasks.render_alert_audio.run(str(result.alert_id))
    alert = AudioAlert.objects.get(id=result.alert_id)
    out = AudioAlertSerializer(alert).data
    assert out["audio_url"]


# ---------------------------------------------------------------------------
# services.py
# ---------------------------------------------------------------------------


def test_has_subscription_returns_false_when_no_row(
    make_user, make_farm
) -> None:
    user = make_user()
    farm = make_farm(user)
    assert (
        has_subscription(
            user_id=user.id,
            farm_id=farm.id,
            alert_type=AudioAlertType.NDVI_LOW,
        )
        is False
    )


def test_emit_audio_alert_event_returns_zero_when_no_layer() -> None:
    with patch("alerts.services.get_channel_layer", return_value=None):
        n = emit_audio_alert_event(1, {"x": "y"})
    assert n == 0


def test_save_audio_bytes_is_a_noop_for_empty() -> None:
    user = _make_user()
    alert = AudioAlert.objects.create(
        user=user,
        alert_type=AudioAlertType.ADMIN_BROADCAST,
        trigger_source="admin_view",
        title="t",
        message="m",
    )
    _save_audio_bytes(alert, b"")
    alert.refresh_from_db()
    assert not alert.audio_file


def test_dispatch_alert_handles_push_failure() -> None:
    """When the channel layer raises, the row is still saved but
    ``is_delivered`` stays False (sent=False branch)."""
    from unittest.mock import MagicMock

    from alerts import tasks

    user = _make_user()
    with (
        patch(
            "alerts.tts.synthesize",
            return_value=TTSResult(b"RIFF", "audio/wav", 100),
        ),
        patch(
            "alerts.services.emit_audio_alert_event",
            side_effect=RuntimeError("ws down"),
        ),
        patch.object(tasks.render_alert_audio, "delay", MagicMock()),
    ):
        result = dispatch_alert(
            user_id=user.id,
            farm_id=None,
            alert_type=AudioAlertType.ADMIN_BROADCAST,
            trigger_source="admin_view",
            title="t",
            message="m",
        )
    assert result.delivered is False
    alert = AudioAlert.objects.get(id=result.alert_id)
    assert alert.is_delivered is False


def test_absolute_audio_url_returns_empty_for_missing_file() -> None:
    user = _make_user()
    alert = AudioAlert.objects.create(
        user=user,
        alert_type=AudioAlertType.ADMIN_BROADCAST,
        trigger_source="admin_view",
        title="t",
        message="m",
    )
    assert _absolute_audio_url(alert) == ""


# ---------------------------------------------------------------------------
# tasks.py
# ---------------------------------------------------------------------------


def test_scan_ndvi_declines_skips_when_build_raises() -> None:
    owner = _make_user()
    from farms.models import Farm

    Farm.objects.create(
        owner=owner, name="F", centroid_lat=0.0, centroid_lon=0.0
    )
    with (
        patch(
            "ndvi.farm_state.build_farm_state",
            side_effect=RuntimeError("ndvi down"),
        ),
        patch("alerts.triggers.on_ndvi_decline") as mocked,
    ):
        res = scan_ndvi_declines()
    assert res["dispatched"] == 0
    mocked.assert_not_called()


def test_scan_ndvi_declines_skips_when_dispatch_raises() -> None:
    owner = _make_user()
    from farms.models import Farm

    Farm.objects.create(
        owner=owner, name="F", centroid_lat=0.0, centroid_lon=0.0
    )
    decline = MagicMock(state="decline")
    with (
        patch("ndvi.farm_state.build_farm_state", return_value=decline),
        patch(
            "alerts.triggers.on_ndvi_decline",
            side_effect=RuntimeError("dispatch down"),
        ),
    ):
        res = scan_ndvi_declines()
    assert res["dispatched"] == 0


def test_scan_low_ndvi_skips_when_no_observation() -> None:
    owner = _make_user()
    from farms.models import Farm

    Farm.objects.create(
        owner=owner, name="F", centroid_lat=0.0, centroid_lon=0.0
    )
    with (
        patch("ndvi.models.NdviObservation") as obs_cls,
        patch("alerts.triggers.on_ndvi_low") as mocked,
    ):
        manager = MagicMock()
        first_mock = manager.valid.return_value.filter.return_value
        first_mock.order_by.return_value.first.return_value = None
        obs_cls.objects = manager
        res = scan_low_ndvi_observations()
    assert res["dispatched"] == 0
    mocked.assert_not_called()


def test_scan_low_ndvi_dedupes_within_24h(make_user, make_farm) -> None:
    user = make_user()
    farm = make_farm(user)
    AudioAlert.objects.create(
        user=user,
        farm=farm,
        alert_type=AudioAlertType.NDVI_LOW,
        trigger_source="ndvi_task",
        title="old",
        message="old",
    )
    fake_obs = MagicMock(id=42, mean=0.1)
    with (
        patch("ndvi.models.NdviObservation") as obs_cls,
        patch("alerts.triggers.on_ndvi_low") as mocked,
    ):
        manager = MagicMock()
        first_mock = manager.valid.return_value.filter.return_value
        first_mock.order_by.return_value.first.return_value = fake_obs
        obs_cls.objects = manager
        res = scan_low_ndvi_observations()
    assert res["dispatched"] == 0
    mocked.assert_not_called()


def test_scan_low_ndvi_skips_when_dispatch_raises() -> None:
    owner = _make_user()
    from farms.models import Farm

    Farm.objects.create(
        owner=owner, name="F", centroid_lat=0.0, centroid_lon=0.0
    )
    fake_obs = MagicMock(id=42, mean=0.1)
    with (
        patch("ndvi.models.NdviObservation") as obs_cls,
        patch(
            "alerts.triggers.on_ndvi_low",
            side_effect=RuntimeError("dispatch down"),
        ),
    ):
        manager = MagicMock()
        first_mock = manager.valid.return_value.filter.return_value
        first_mock.order_by.return_value.first.return_value = fake_obs
        obs_cls.objects = manager
        res = scan_low_ndvi_observations()
    assert res["dispatched"] == 0


# ---------------------------------------------------------------------------
# triggers.py
# ---------------------------------------------------------------------------


def test_on_activity_completed_swallows_dispatch_error(
    make_user, make_farm
) -> None:
    user = make_user()
    farm = make_farm(user)
    AudioAlertSubscription.objects.create(
        user=user,
        farm=farm,
        alert_types=[AudioAlertType.ACTIVITY_COMPLETED],
    )
    with patch(
        "alerts.triggers.dispatch_alert",
        side_effect=RuntimeError("dispatch down"),
    ):
        on_activity_completed(
            user_id=user.id,
            farm_id=farm.id,
            activity_id=1,
            activity_type="irrigation",
            status="success",
            message="x",
        )


def test_on_admin_broadcast_swallows_dispatch_error() -> None:
    user = _make_user()
    with patch(
        "alerts.triggers.dispatch_alert",
        side_effect=RuntimeError("dispatch down"),
    ):
        n = on_admin_broadcast(recipients=[user.id], title="t", message="m")
    assert n == 0


def test_on_ndvi_fan_out_skips_owner_when_none(make_user, make_farm) -> None:
    """``_fan_out`` works with ``owner_id=None`` (149->151 branch)."""
    user = make_user()
    farm = make_farm(user)
    AudioAlertSubscription.objects.create(
        user=user,
        farm=farm,
        alert_types=[AudioAlertType.NDVI_DECLINE],
    )
    with patch("alerts.triggers.dispatch_alert") as mocked:
        n = on_ndvi_decline(farm_id=farm.id, owner_id=None, message="x")
    assert n == 1
    assert mocked.call_count == 1


def test_on_ndvi_low_swallows_dispatch_error(make_user, make_farm) -> None:
    user = make_user()
    farm = make_farm(user)
    with patch(
        "alerts.triggers.dispatch_alert",
        side_effect=RuntimeError("dispatch down"),
    ):
        n = on_ndvi_low(farm_id=farm.id, owner_id=user.id, message="x")
    assert n == 0


# ---------------------------------------------------------------------------
# tts.py
# ---------------------------------------------------------------------------


def test_piper_runs_with_binary_on_path() -> None:
    """_piper happy path: piper on PATH, exits 0, valid WAV returned."""
    fake_wav = _sine_wav(duration_s=0.05, freq=440)
    fake_proc = MagicMock(returncode=0, stderr=b"")
    with (
        patch("alerts.tts.shutil.which", return_value="/usr/bin/piper"),
        patch("subprocess.run", return_value=fake_proc),
        patch.object(
            __import__("alerts.tts", fromlist=["Path"]).Path,
            "read_bytes",
            return_value=fake_wav,
        ),
    ):
        out = _piper("hi", voice="en", timeout_s=1.0)
    assert out.audio_bytes == fake_wav
    assert out.mime_type == "audio/wav"
    assert out.duration_ms > 0


def test_piper_raises_on_nonzero_exit() -> None:
    fake_proc = MagicMock(returncode=1, stderr=b"boom")
    with (
        patch("alerts.tts.shutil.which", return_value="/usr/bin/piper"),
        patch("subprocess.run", return_value=fake_proc),
    ):
        with pytest.raises(RuntimeError):
            _piper("hi", voice="en", timeout_s=1.0)


def test_espeak_runs_with_binary_on_path() -> None:
    fake_wav = _sine_wav(duration_s=0.05, freq=440)
    fake_proc = MagicMock(returncode=0, stderr=b"")
    with (
        patch("alerts.tts.shutil.which", return_value="/usr/bin/espeak"),
        patch("subprocess.run", return_value=fake_proc),
        patch.object(
            __import__("alerts.tts", fromlist=["Path"]).Path,
            "read_bytes",
            return_value=fake_wav,
        ),
    ):
        out = _espeak("hi", voice="en", timeout_s=1.0)
    assert out.audio_bytes == fake_wav


def test_espeak_raises_on_nonzero_exit() -> None:
    fake_proc = MagicMock(returncode=2, stderr=b"boom")
    with (
        patch("alerts.tts.shutil.which", return_value="/usr/bin/espeak"),
        patch("subprocess.run", return_value=fake_proc),
    ):
        with pytest.raises(RuntimeError):
            _espeak("hi", voice="en", timeout_s=1.0)


def test_guess_wav_duration_returns_none_on_invalid_input() -> None:
    """When the bytestream is not a valid WAV, the helper returns None
    (covers the ``wave.Error / EOFError / ValueError`` catch-all)."""
    assert _guess_wav_duration_ms(b"not wav") is None


def test_synthesize_falls_back_when_backend_raises() -> None:
    """synthesize catches backend exception and returns sine fallback
    (line 205-211 branch). The fallback calls ``_sine_fallback`` directly,
    not the ``_BACKENDS['sine']`` entry, so we assert the result is a
    non-empty sine WAV."""
    failing = MagicMock(side_effect=RuntimeError("backend boom"))
    with (
        patch("alerts.tts._BACKENDS", {"espeak": failing}),
        override_settings(
            TTS_ENGINE="espeak", TTS_VOICE="en", TTS_TIMEOUT_SECONDS=1.0
        ),
    ):
        out = synthesize("hi")
    assert out.audio_bytes
    assert out.mime_type == "audio/wav"
    assert out.duration_ms > 0


def test_synthesize_truncates_long_text() -> None:
    with override_settings(
        TTS_ENGINE="noop", TTS_VOICE="en", TTS_MAX_TEXT_CHARS=5
    ):
        out = synthesize("this is a long message that exceeds five")
    assert out.audio_bytes == b""
    assert out.duration_ms == 0


def test_synthesize_logs_timing() -> None:
    """Cover the ``logger.info`` post-call summary line."""
    with override_settings(
        TTS_ENGINE="noop", TTS_VOICE="en", TTS_TIMEOUT_SECONDS=1.0
    ):
        with patch("alerts.tts.logger") as log:
            synthesize("hi")
    assert log.info.called


# ---------------------------------------------------------------------------
# views.py
# ---------------------------------------------------------------------------


def test_subscription_delete_returns_404_when_missing() -> None:
    user = _make_user()
    client = APIClient()
    client.force_authenticate(user=user)
    from uuid import uuid4

    resp = client.delete(f"/api/v1/alerts/subscriptions/{uuid4()}/")
    assert resp.status_code == 404


def test_alert_list_with_invalid_limit_falls_back() -> None:
    user = _make_user()
    client = APIClient()
    client.force_authenticate(user=user)
    resp = client.get("/api/v1/alerts/?limit=notanumber")
    assert resp.status_code == 200
    assert resp.data["status"] == 0


def test_alert_acknowledge_returns_404_when_missing() -> None:
    user = _make_user()
    client = APIClient()
    client.force_authenticate(user=user)
    from uuid import UUID

    resp = client.post(
        f"/api/v1/alerts/{UUID('00000000-0000-0000-0000-000000000000')}/"
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# real WAV sanity check (also exercises _guess_wav_duration_ms happy path)
# ---------------------------------------------------------------------------


def test_guess_wav_duration_reads_real_wav() -> None:
    """When given a real WAV the helper returns a positive int."""
    buf = BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 1600)
    ms = _guess_wav_duration_ms(buf.getvalue())
    assert ms == 100
