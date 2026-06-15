# NDVI Pipeline Evolution: Redis Sentinel + Streams (Phased Approach)

**Date:** March 24, 2026  
**System summary:** Django + DRF (`farm-intelligence-platform`) running Celery 5.6.2, backed by a single-node Redis instance used for broker/result/cache, and the separate Rust-based `ndvi-service` proxy (ports 8081/8090) that feeds farm-state coverage data back into Django.

> Historical architecture snapshot captured during the phased rollout design.
> Current implementation details may have moved ahead of the notes below.
> For the consolidated NDVI architecture and implementation spec, see `docs/architecture/ndvi-system-evolution-phased-spec.md`.
> For live status, see `docs/status/NDVI_PIPELINE_IMPLEMENTATION_STATUS.md`.

## 1. Problem statement
- **Single-node Redis = SPOF:** A Redis outage halts Celery and makes `/api/v1/farm-state/{farm_id}` unreliable because broker, result backend, and cache all live on that node.
- **Celery sensitivity:** Grafana shows P95 spikes even under modest traffic, indicating Celery queues are fragile when Redis performance degrades (no automatic failover).
- **False latency visibility:** The latency dashboards plot flat lines for inactive endpoints because they lack reliable backlog/queue metrics, so we can’t distinguish real workload from stale data.

## 2. Proposed architecture
- **Redis Sentinel** for HA broker/cache/result backend.
- **Redis Streams** for NDVI ingestion (targeted queue only) while retaining Celery for general async tasks.
- **Kafka deferred**; it will only be introduced once explicit thresholds (see Phase 4) are met.

## 3. Phased rollout plan

### Phase 1 – Redis Sentinel (Immediate – March 24, 2026)
**Objective:** Remove the Redis SPOF with minimal behavior change.

- Deploy a Redis Sentinel trio.
- Update `.env` URLs (`CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`, `DJANGO_CACHE_URL`, `REDIS_URL`) to Sentinel URIs (`redis-sentinel://...` with `service_name`).
- **Failover validation:** stop the master, confirm Sentinel election, observe Celery reconnect logs, and run farm-state tasks during failover to ensure success.
- **Checkpoint:** Sentinel metrics (`redis_sentinel_master_status`, `redis_sentinel_master_ok_sentinels`) are in Prometheus before moving on.

#### Known Limitations (Added April 2026)
- **Celery failover recovery time:** ~55 seconds
  - Acceptable for background jobs (NDVI, farm state coverage)
  - Not suitable for latency-sensitive task dispatch (<10-15s target)
  - If real-time dispatch is required, tune Celery reconnect behavior in future work
- **URL conversion complexity:** `config/settings.py` converts `redis-sentinel://` to `sentinel://` for Celery compatibility; monitor for breakage on Celery upgrades

### Phase 2 – Redis Streams for NDVI (Next phase)
**Objective:** Give NDVI coverage jobs durable, observable queue semantics without touching the rest of Celery.

- ✅ **Stage 1 dispatch centralization is already complete in code.**
- The current code keeps `NDVI_QUEUE_BACKEND=celery` as the default and supports `stream` as an opt-in Redis Streams transport.
- The implementation uses a separate Redis Streams producer/consumer path rather than Celery/Kombu stream transport.
- Stream entries are produced by `ndvi/streams.py` and consumed by `ndvi/management/commands/consume_ndvi_stream.py`.
- Consumers:
  - `XREADGROUP` → process task → `XACK`.
  - Use `XAUTOCLAIM` and `XPENDING` to recover stale entries.
  - Send persistent failures to a dead-letter stream trimmed by `XTRIM`.
- Back-pressure/retry handling:
  - Monitor `XPENDING` thresholds; throttle producers/backoff when backlog grows.
  - Trim main stream (e.g., `MAXLEN ~10000` or 12h) and dead-letter stream (e.g., 100 entries/7 days).

### Phase 3 – Observability (MERGED into Phase 2)

**Status:** Observability is not a separate phase. It must be implemented alongside
the stream producer/consumer to ensure stream adoption is measurable from day one.

All Phase 3 requirements are now tracked in Phase 2 Stage 6 of the implementation plan.

**Original requirements (now in Phase 2):**
- Export Prometheus metrics for stream health
- Update Grafana dashboards with stream lag panels
- Add alerting for stream lag + Celery failures

### Phase 4 – Kafka (Future / Conditional)
**Objective:** Transition to Kafka only if scale demands it.

- Kafka adoption triggers:
  1. Stream lag remains high despite adding consumers (XPENDING >> throughput).
  2. Need for durable replay across multiple services (NDVI, Nextcloud, analytics).
  3. Demand for partitioned/fan-out consumption beyond Redis Streams' capabilities.
- Once triggered, Kafka topics (`ndvi-requests`, `ndvi-results`) would replace the stream queue.

#### Re-evaluation Schedule (Added April 2026)
Re-assess Kafka need when:
1. Redis stream lag consistently > 1000 entries for 7+ days
2. Pending age > 5× job runtime for 7+ days
3. Multiple services require NDVI data fan-out
4. Replay requirements exceed Redis Streams capabilities

**Earliest re-evaluation date:** Q3 2026 (6 months after stream rollout)

## 4. Operational considerations
- **Failure modes:**
  - Sentinel: watch `redis_sentinel_master_status`, `redis_sentinel_master_ok_sentinels`, and the `master_address` label; expect short-lived `celery_broker_disconnects`.
  - Streams: monitor `XPENDING`, pending age; reclaim stale entries and use a DLQ for repeated failures.
- **Rollback:** The stream queue configuration is a simple Celery flag; revert to list-backed behavior if needed with no code changes.
- **Memory/retention:** Trim streams via `MAXLEN`/`XTRIM`, keep Redis maxmemory/eviction policies, and pause producers when `XPENDING` exceeds thresholds.

## 5. Decision log
- **Why Redis Streams over Kafka now:** Current workload is bursty but not at Kafka scale. Streams stay in Redis, require minimal code/config change, and give durable ingestions.
- **Why Kafka is deferred:** Kafka’s operational cost is unjustified until explicit thresholds are met (stream lag despite scaling, need for replay/fan-out).
- **Kafka adoption metrics:** High `redis_stream_pending_entries` despite consumer scale, pending age > 5× job runtime, or explicit replay/fan-out requirements.

## 6. ⚠️ Open technical question
- **Does Celery/Kombu 5.6.2 fully support Redis Streams via `transport_options={"stream": True}` in a production-safe way?**
  - If not, the **separate Redis Streams consumer (outside Celery)** is not merely a fallback but a valid alternative architecture. In that model, NDVI stream consumers (Python/Rust) read entries, enforce the same job idempotency, and enqueue work onto a standard Celery queue. This design may be preferred if Celery’s Streams support is limited or unstable.

## 7. Summary
- Sentinel + Streams resolve the current Redis SPOF + visibility gap without rearchitecting Celery or rushing to Kafka.
- Phased checkpoints (Sentinel validation, stream pilot, observability updates) keep rollouts reversible and measurable.
- Kafka waits on defined thresholds, letting us solve today’s constraints without premature complexity.
