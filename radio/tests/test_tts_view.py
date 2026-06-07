"""Tests for the radio TTS view.

Covers the thin radio-side wrapper around ``alerts.tts.synthesize``.
The TTS backends are stubbed so we do not shell out to espeak/piper
during tests.
"""

from __future__ import annotations

import secrets
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from alerts.tts import TTSResult

User = get_user_model()


class TTSSynthesizeViewTestCase(TestCase):
    """Endpoint coverage for ``POST /api/v1/radio/tts/``."""

    def setUp(self) -> None:
        self.client_api = APIClient()
        self.user = User.objects.create_user(
            username="carol", password=secrets.token_urlsafe(12)
        )

    def test_requires_authentication(self) -> None:
        response = self.client_api.post(
            reverse("radio-tts"),
            data={"text": "hello"},
            format="json",
        )
        self.assertEqual(response.status_code, 401)

    def test_synthesizes_and_returns_base64(self) -> None:
        self.client_api.force_authenticate(self.user)
        fake = TTSResult(b"\x00\x01\x02RIFF", "audio/wav", 1234)
        with patch("alerts.tts.synthesize", return_value=fake) as mocked:
            response = self.client_api.post(
                reverse("radio-tts"),
                data={"text": "Hello world"},
                format="json",
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], 0)
        self.assertEqual(body["data"]["mime_type"], "audio/wav")
        self.assertEqual(body["data"]["duration_ms"], 1234)
        self.assertEqual(
            body["data"]["audio_base64"],
            "AAECUklGRg==",
        )
        mocked.assert_called_once()

    def test_rejects_empty_text(self) -> None:
        self.client_api.force_authenticate(self.user)
        response = self.client_api.post(
            reverse("radio-tts"),
            data={"text": "   "},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_rejects_oversized_text(self) -> None:
        self.client_api.force_authenticate(self.user)
        with self.settings(TTS_MAX_TEXT_CHARS=10):
            response = self.client_api.post(
                reverse("radio-tts"),
                data={"text": "x" * 50},
                format="json",
            )
        self.assertEqual(response.status_code, 400)
