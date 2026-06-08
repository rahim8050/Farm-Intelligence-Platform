# Alerts App

Back to root: `../README.md`

## Overview

This app provides **per-user, per-farm audio alerts** for the
Django + Nextcloud ecosystem.

It is responsible for:
- TTS-driven audio generation for short alert messages.
- A per-user, per-farm subscription model that decides which alert
  types the user receives.
- A best-effort WebSocket push to the user's existing Channels
  group, plus a REST polling fallback (`GET /api/v1/alerts/`) for
  clients that were offline when the alert was raised.
- Three trigger sources: activity completion (wired into
  `activities/tasks.py`), periodic NDVI-decline scans, and
  periodic low-NDVI-absolute scans. Admins can also send
  manual broadcasts.

It is not responsible for:
- Audio transcoding or playback (clients stream the WAV URL
  directly — same as `radio/` and `podcasts/`).
- Multi-language TTS (a single `TTS_VOICE` is configured at
  the project level).
- Push to mobile (only WebSocket / REST; no FCM/APNS).

## API surface

Base path: `/api/v1/alerts/`.

All successful responses use the project envelope produced by
`config.api.responses.success_response`:

```json
{
  "status": 0,
  "message": "string",
  "data": {},
  "errors": null,
  "request_id": "req_..."
}
```

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET | `/api/v1/alerts/subscriptions/` | Auth | List the caller's subscriptions. |
| POST | `/api/v1/alerts/subscriptions/` | Auth | Create or upsert a subscription (idempotent). |
| PATCH | `/api/v1/alerts/subscriptions/<id>/` | Auth | Update alert_types on a subscription. |
| DELETE | `/api/v1/alerts/subscriptions/<id>/` | Auth | Remove a subscription. |
| GET | `/api/v1/alerts/?unread=true&limit=100` | Auth | List the caller's alerts. |
| GET | `/api/v1/alerts/<id>/` | Auth | Get a single alert (including its audio URL). |
| POST | `/api/v1/alerts/<id>/` | Auth | Mark an alert as acknowledged. |
| POST | `/api/v1/alerts/admin/send/` | Admin | Send a manual broadcast to a list of users. |

## Data model

| Model | Notes |
| --- | --- |
| `AudioAlertSubscription` | UUID PK, `user`, `farm` (FK), `alert_types: list[str]`, unique `(user, farm)`, indexed on `(user, farm)`. |
| `AudioAlert` | UUID PK, `user`, `farm` (FK, nullable), `alert_type`, `trigger_source`, `title`, `message`, `audio_file` (FileField under `MEDIA_ROOT/audio_alerts/...`), `duration_ms`, `mime_type`, `source_object_id`, `is_delivered`, `is_acknowledged`, `delivered_at`, `acknowledged_at`, `created_at`. Indexed on `(user, -created_at)`, `(user, is_acknowledged)`, `(alert_type, -created_at)`. |

## TTS engines

`alerts/tts.py` is a pluggable backend; select with `TTS_ENGINE`:

| Engine | Notes |
| --- | --- |
| `piper` | High-quality neural TTS via the `piper` CLI / `piper-tts` package. Falls back to `espeak` if the binary is not on `PATH`. |
| `espeak` | Low-fidelity but ubiquitous; shells out to `espeak-ng` or `espeak`. Default. |
| `sine` | Always-on 440Hz 1s tone (WAV). Useful when no TTS engine is installed. |
| `noop` | Returns empty bytes. Used in tests / CI. |

Every backend returns a `TTSResult(audio_bytes, mime_type, duration_ms)`.
A backend failure is logged and the function falls back to the
sine generator, so the system always produces a row.

## Triggers

| Source | Where |
| --- | --- |
| Activity completion | `activities/tasks.py:execute` calls `alerts.triggers.on_activity_completed` on `transaction.on_commit`. |
| NDVI decline | `alerts.tasks.scan_ndvi_declines` (beat, every `ALERTS_NDVI_DECLINE_SCAN_INTERVAL_SECONDS`). Reads `build_farm_state` from `ndvi/farm_state.py`. |
| Low NDVI absolute | `alerts.tasks.scan_low_ndvi_observations` (same beat schedule). Reads latest `NdviObservation.mean` against `ALERTS_NDVI_LOW_THRESHOLD` (default 0.2). |
| Admin broadcast | `POST /api/v1/alerts/admin/send/` (admin only). |

De-duplication: a farm does not receive a second alert of the
same type within a 24h window. Implemented as a `created_at`
filter in the scan tasks; not a separate table.

## WebSocket push

The existing `activities.consumers.ActivityConsumer` is extended
with an `audio_alert` handler (`activities/consumers.py:97-115`).
The group name is `user_<user.id>` (controlled by
`ALERTS_WEBHOOK_GROUP_PREFIX`, default `user_`), so a single
WS connection carries both `activity_event` and `audio_alert`
frames.

The push is best-effort: a send failure does not affect the
persisted `AudioAlert` row. Clients that reconnect later can
catch up via `GET /api/v1/alerts/?unread=true`.

## Settings

| Setting | Default | Purpose |
| --- | --- | --- |
| `TTS_ENGINE` | `espeak` | One of `piper` / `espeak` / `sine` / `noop`. |
| `TTS_VOICE` | `en` | Voice / language passed to the TTS engine. |
| `TTS_TIMEOUT_SECONDS` | `10.0` | Per-call TTS timeout. |
| `TTS_MAX_TEXT_CHARS` | `500` | Truncate the message before synthesis. |
| `ALERTS_NDVI_DECLINE_SCAN_INTERVAL_SECONDS` | `900` | Beat schedule for both NDVI scans. |
| `ALERTS_NDVI_LOW_THRESHOLD` | `0.2` | Absolute NDVI threshold for the low-NDVI scan. |
| `ALERTS_WEBHOOK_GROUP_PREFIX` | `user_` | Prefix for the Channels group. |

Throttle scope: `alerts_admin: 30/min` (admin broadcast endpoint).

## Testing

```bash
pytest alerts/tests
```

Coverage: TTS backend, alert dispatch, idempotent acknowledge,
REST CRUD, admin broadcast, NDVI scan de-duplication, and the
trigger helpers. TTS and the channel layer are stubbed in
`alerts/tests/conftest.py` and `alerts/tests/test_services.py`
so the suite is hermetic.

## Documentation

See `docs/architecture/radio/IMPLEMENTATION_SUMMARY.md` § Phase 4
audio alerts and `docs/architecture/radio/08_future_expansion.md`
for the original P4 design sketch that this implementation
followed.
