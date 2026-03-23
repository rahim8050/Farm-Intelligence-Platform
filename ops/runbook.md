# Ops Runbook Notes

This runbook captures the future rollout path for Queue/NDVI resiliency:

- **Primary reference:** [`docs/architecture/ndvi-pipeline-evolution.md`](../docs/architecture/ndvi-pipeline-evolution.md) documents the Sentinel + Redis Streams plan, blocked Phase 2, observability milestones, and Kafka adoption triggers.
- **Usage:** Consult the linked architecture doc before making changes to Redis, Celery tasks, or NDVI ingestion pipelines to keep the execution path controlled and reversible.
