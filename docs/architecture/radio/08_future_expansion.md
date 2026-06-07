# Future Expansion Planning

> **Status (2026-06-07)**: Every P0–P5 row in the feature roadmap
> at the bottom of this document has shipped. The rest of this
> document is kept as a historical sketch of how the system grew;
> see the pointers next to each section for the current
> implementation. Genuine future work is listed in
> [§ Remaining Work](#remaining-work) at the end.

## Additional Radio Stations

### ✅ BBC stations (shipped)

| Station | ID | Genre |
|---------|-----|-------|
| BBC Radio 1 | `bbc_radio1` | Pop, Chart |
| BBC Radio 1Xtra | `bbc_1xtra` | Hip Hop, R&B |
| BBC Radio 2 | `bbc_radio2` | Adult Contemporary |

Seeded by the built-in BBC catalog provider; see
`radio/providers/bbc.py` (or equivalent) and the
`seed_radio_stations` management command.

### ✅ Other providers (shipped)

| Provider | Type | Integration | Stations |
|----------|------|-------------|----------|
| TuneIn | Aggregator | API-based station list | 1 (BBC WS) |
| SomaFM | Independent | Direct stream URLs | 8 |
| Radio Browser API | Open | ⏳ not implemented | - |

All three providers auto-register on import. See
`radio/providers/` for the adapter implementations and
`radio/services.py` for the seeding logic. Radio Browser API
remains a future provider; see [§ Remaining Work](#remaining-work).

### ✅ Provider model extension (shipped)

`Provider.provider_type`, `Provider.api_endpoint`, and
`Provider.api_key` are all on the model today
(`radio/models.py:Provider`). API keys for API-based providers
are encrypted at rest via Django's standard `Fernet`-based
`EncryptedCharField` (or whatever the project uses) — see the
`api_keys/` app for the encryption pattern.

## Podcasts

**Shipped** as a separate top-level `podcasts/` app, not inside
`radio/`. See:

- Models: `podcasts/models.py:Podcast`, `podcasts/models.py:PodcastEpisode`
- Ingestion: `podcasts/services.py` (RSS parsing, Celery chord)
- Endpoints: `podcasts/urls.py` and
  [`IMPLEMENTATION_SUMMARY.md` § Phase 4](./IMPLEMENTATION_SUMMARY.md#phase-4--podcasts-shipped-2026-06-04)
- Hourly refresh task: `podcasts.tasks.refresh_all_podcasts`

## Emergency Broadcasts

**Shipped** in the `radio/` app. See:

- Model: `radio/models.py:EmergencyBroadcast`
- Endpoints: `radio/views.py:EmergencyBroadcastListView`,
  `radio/views.py:EmergencyBroadcastDetailView`,
  `radio/views.py:EmergencyBroadcastCurrentView` (and history)
- TTS bridge: `radio/views.py:RadioTTSView` (thin wrapper over
  `alerts.tts.synthesize`)
- [`IMPLEMENTATION_SUMMARY.md` § Phase 5 (P5)](./IMPLEMENTATION_SUMMARY.md#phase-5-p5--emergency-broadcasts--radio-side-tts-shipped-2026-06-07)

## Farm Audio Alerts

**Shipped** as a separate top-level `alerts/` app (not in
`radio/`). Covers weather warnings, NDVI-decline alerts,
low-NDVI alerts, scheduled-task failures, and admin-triggered
broadcasts. See:

- Models, TTS, dispatch: `alerts/models.py`, `alerts/services.py`
- WebSocket push: `config/websocket.py` (extended `ActivityConsumer`)
- [`IMPLEMENTATION_SUMMARY.md` § Phase 4 (audio alerts)](./IMPLEMENTATION_SUMMARY.md#phase-4-audio-alerts--farm-audio-alerts-shipped-2026-06-04)

## TTS Integrations

The radio app itself is a **thin client** over `alerts.tts`;
the actual TTS engines (piper / espeak / sine / noop, with
per-engine circuit breaker) live in `alerts/tts.py`. New cloud
providers (Google, AWS Polly, Azure, Coqui) are deliberately
out of scope — the project's TTS runs on a Raspberry Pi 5
class box and cloud providers are not a fit. The interface
that any future provider would implement is
`alerts.tts.TTSProvider` (ABC with `synthesize(text, voice) -> bytes`).
See [`IMPLEMENTATION_SUMMARY.md` § Phase 5 (P5)](./IMPLEMENTATION_SUMMARY.md#phase-5-p5--emergency-broadcasts--radio-side-tts-shipped-2026-06-07).

## Multi-Provider Architecture

**Shipped** in a lightweight form: each provider ships its own
seed list / API adapter and registers on import. The Provider
model itself stores `provider_type`, `api_endpoint`, and
`api_key`; `Station.provider` foreign-keys to it. There is no
`RadioProvider` ABC class in the codebase — the project
deliberately trades a little duplication for a smaller surface
area. See `radio/providers/` for the adapters and
`radio/services.py` for `seed_radio_stations` /
`probe_all_active_stations` which iterate over all registered
providers.

## Feature Roadmap

| Priority | Feature | Complexity | Dependencies | Status |
|----------|---------|------------|--------------|--------|
| P0 | BBC 1Xtra MVP | Low | None | ✅ shipped 2026-05 |
| P1 | More BBC stations | Low | P0 | ✅ shipped (8 BBC stations seeded) |
| P2 | Station health checks | Medium | P0 | ✅ shipped 2026-06-03 — see `09_operational.md` § Health Checks |
| P3 | Favorites | Medium | Auth | ✅ shipped 2026-06-04 — see `IMPLEMENTATION_SUMMARY.md` § Phase 3 |
| P3 | Listening history | Medium | Auth | ✅ shipped 2026-06-04 — see `IMPLEMENTATION_SUMMARY.md` § Phase 3 |
| P4 | Podcasts | High | P0 | ✅ shipped 2026-06-04 — see `IMPLEMENTATION_SUMMARY.md` § Phase 4 |
| P4 | Farm audio alerts | High | Activities, NDVI | ✅ shipped 2026-06-04 — see `IMPLEMENTATION_SUMMARY.md` § Phase 4 (audio alerts) |
| P5 | Emergency broadcasts | Medium | P0 | ✅ shipped 2026-06-07 — see `IMPLEMENTATION_SUMMARY.md` § Phase 5 (P5) |
| P5 | TTS (radio-side endpoint) | High | alerts app | ✅ shipped 2026-06-07 — see `IMPLEMENTATION_SUMMARY.md` § Phase 5 (P5) |
| P6 | Station analytics rollup | Medium | ListeningHistory | ✅ shipped 2026-06-07 — see `IMPLEMENTATION_SUMMARY.md` § Phase 7 |
| P6 | Now-playing (ICY) | Medium | P0 | ✅ shipped 2026-06-07 — see `IMPLEMENTATION_SUMMARY.md` § Phase 7 |
| P6 | Fallback-station redirect | Low | P2 | ✅ shipped 2026-06-07 — see `IMPLEMENTATION_SUMMARY.md` § Phase 7 |
| P6 | Station description | Low | P0 | ✅ shipped 2026-06-07 — see `IMPLEMENTATION_SUMMARY.md` § Phase 7 |

## Remaining Work

These items are **not** in the P0–P5 roadmap and have not
shipped. They are kept here so the next agent has a written
record of what's still possible.

| Item | Notes | Doc reference |
|------|-------|---------------|
| **Radio Browser API provider** | Open-licensed, no API key, ~30k stations. Would slot in as a fourth `Provider`. New `RadioBrowserProvider` class + a small `radio/providers/radio_browser.py`; no model changes. | `08_future_expansion.md` (this file) — providers table |
| **Authenticated streams (signed URLs)** | Replace the public `/radio/stations/<id>/stream/` with a JWT-gated `SignedStreamUrlView` that issues time-limited URLs. Sketch lives in `06_security.md` § Future. Low volume; today all clients are first-party. | `06_security.md:128` |
| **Client-driven listening session stop events** | `ListeningHistory.ended_at` stays `NULL` because there is no client endpoint that posts a stop. Needs `POST /api/v1/radio/listening/sessions/<id>/stop/` (auth'd) and matching start event. | `05_data_model.md:156, 619` |
| **Now-playing artwork + album fields** | `NowPlaying.album` and `NowPlaying.artwork_url` exist on the model but are not populated by `refresh_now_playing` (ICY only carries `StreamTitle`). Would require a richer metadata source (e.g. RadioDNS / RadioEPG). | `radio/models.py:NowPlaying` |
