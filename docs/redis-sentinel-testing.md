# Redis Sentinel Testing Phases

Documenting how to validate the Redis Sentinel setup for this project using Docker: phases map to the rollout notes in `docs/architecture/ndvi-pipeline-evolution.md` and the sentinel-aware URL parsing in `config/settings.py`.

## Phase 1 – Stand up the Sentinel stack

1. Create a dedicated `docker compose` file (e.g., `docker-compose.sentinel.yml`) that runs:
   - A Redis master service exposing `6379` with `redis-server` and a simple `redis.conf`.
   - One or more replicas launched as slaves of the master.
   - Three Sentinel services (Sentinel requires a quorum) pointing at the service name you plan to use (`mymaster`) and the master port. Mount a `sentinel.conf` that contains:
     ```ini
     sentinel monitor mymaster redis-master 6379 2
     sentinel down-after-milliseconds mymaster 5000
     sentinel failover-timeout mymaster 10000
     sentinel parallel-syncs mymaster 1
     ```
2. Start the stack with `docker compose -f docker-compose.sentinel.yml up -d` and wait for the health checks to pass.
3. Verify the sentinel trio has registered the master:
   ```bash
   docker compose -f docker-compose.sentinel.yml exec sentinel1 redis-cli -p 26379 sentinel get-master-addr-by-name mymaster
   ```
   The output should show the master host/port.
4. (Optional) Add `redis_exporter` configured with `REDIS_ADDR=redis://sentinel1:26379` to exercise the metrics that populate Prometheus dashboards in the architecture plan.

## Phase 2 – Point Django/Celery at Sentinel

1. Update your local `.env` or test environment to use sentinel-aware URLs, mirroring the README guidance:
   ```dotenv
   REDIS_URL=redis-sentinel://sentinel1:26379;sentinel2:26379/0?service_name=mymaster
   DJANGO_CACHE_URL=${REDIS_URL}
   CELERY_BROKER_URL=${REDIS_URL}
   CELERY_RESULT_BACKEND=${REDIS_URL}
   ```
   The `config/settings.py` helper `_parse_redis_sentinel_url` will parse these values and build a `SentinelConnectionFactory` to keep the cache/broker tied to the elected master.
2. Restart the Django app (and any Celery worker/beat processes) so they pick up the new URLs; you can confirm the sentinel config by rerunning `python -m pytest tests/test_settings_redis_sentinel.py`.
3. Trigger a cache-backed API call or enqueue an NDVI/Celery task to exercise the sentinel-backed broker/cache; logs should show connections to the sentinels and ACK/WRITE operations aimed at the master.

## Phase 3 – Failover verification

1. While the stack is still running, stop the master container (`docker compose -f docker-compose.sentinel.yml stop redis-master`).
2. Wait a few seconds for Sentinel to elect a new master and then run:
   ```bash
   docker compose -f docker-compose.sentinel.yml exec sentinel1 redis-cli -p 26379 sentinel get-master-addr-by-name mymaster
   ```
   The host/port should now point to the promoted replica.
3. Execute a cache-dependent API call or Celery task during the failover; the request should succeed once the new master is ready. Celery logs may show reconnects but should not abort.
4. Monitor the Prometheus metrics `redis_master_up` and `sentinel_master_up` (if exporter is attached) to confirm visibility of the election.
5. Restart the original master so it rejoins as a replica, restoring the three-node topology.

## Notes & references

- The sentinel stack aligns with the Phase 1 objective in `docs/architecture/ndvi-pipeline-evolution.md`, and the URL parsing that makes Django/Celery sentinel-aware lives in `config/settings.py`.
- You can reuse this compose file for automated smoke tests or CI targets, ensuring each phase (standup, configuration, failover) has a scripted verification step.
