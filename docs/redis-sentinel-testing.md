# Redis Sentinel Testing Phases

Documenting how to validate the Redis Sentinel setup for this project using Docker: phases map to the rollout notes in `docs/architecture/ndvi-pipeline-evolution.md` and the sentinel-aware URL parsing in `config/settings.py`.

## Phase 1 – Stand up the Sentinel stack

1. Use the provided `docker-compose.redis-sentinel.yml` (root) plus the configs under `ops/redis/` to run a stack containing:
   - A Redis master service exposing `6379` with `redis-server` and a simple `redis.conf`.
   - One or more replicas launched as slaves of the master.
   - Three Sentinel services (Sentinel requires a quorum) that each mount `ops/redis/start-sentinel.sh`. The script waits until `redis-master` resolves on the Docker network, writes a temporary `sentinel.conf` using the IP it discovered, and starts `redis-server --sentinel` with the same monitor/failover settings you would otherwise put in a static config.
     ```ini
     sentinel monitor mymaster <master-ip> 6379 2
     sentinel down-after-milliseconds mymaster 5000
     sentinel failover-timeout mymaster 10000
     sentinel parallel-syncs mymaster 1
     sentinel deny-scripts-reconfig yes
     ```
2. Start the stack with `docker compose --file docker-compose.redis-sentinel.yml up -d` (or drop to `docker-compose` if needed) and wait for the health checks to pass.
3. Verify the sentinel trio has registered the master:
   ```bash
   docker compose --file docker-compose.redis-sentinel.yml exec sentinel1 redis-cli -p 26379 sentinel get-master-addr-by-name mymaster
   ```
   The output should show the master host/port.
4. (Optional) Add `redis_exporter` configured with `REDIS_ADDR=redis://host.docker.internal:26379` (or another published Sentinel port) to exercise the metrics that populate Prometheus dashboards in the architecture plan.

### Phase 1 evidence (sentinel stack)

- `docker compose -p sentinel --file docker-compose.redis-sentinel.yml logs sentinel1 | tail -n 20`
  ```
  sentinel1-1  | resolved redis-master -> 172.20.0.2
  sentinel1-1  | writing sentinel config
  sentinel1-1  | 1:X 01 Apr 2026 15:17:28.460 * oO0OoO0OoO0Oo Redis is starting oO0OoO0OoO0Oo
  sentinel1-1  | 1:X 01 Apr 2026 15:17:28.460 * Redis version=8.6.0, bits=64, commit=00000000, modified=1, pid=1, just started
  sentinel1-1  | 1:X 01 Apr 2026 15:17:28.460 * Configuration loaded
  sentinel1-1  | 1:X 01 Apr 2026 15:17:28.471 * monotonic clock: POSIX clock_gettime
  sentinel1-1  | 1:X 01 Apr 2026 15:17:28.473 * Running mode=sentinel, port=26379.
  sentinel1-1  | 1:X 01 Apr 2026 15:17:28.653 * Sentinel new configuration saved on disk
  sentinel1-1  | 1:X 01 Apr 2026 15:17:28.653 # +monitor master mymaster 172.20.0.2 6379 quorum 2
  sentinel1-1  | 1:X 01 Apr 2026 15:17:28.655 * +slave slave 172.20.0.3:6379 172.20.0.3 6379 @ mymaster 172.20.0.2 6379
  sentinel1-1  | 1:X 01 Apr 2026 15:17:30.141 * +sentinel sentinel 4cbf5c9973171eb5065d34eb2f4e46755b1ad0f5 172.20.0.4 26379 @ mymaster 172.20.0.2 6379
  sentinel1-1  | 1:X 01 Apr 2026 15:17:30.402 * +sentinel sentinel d4d764a422ea9a07e1b750702407218c49c8400b 172.20.0.5 26379 @ mymaster 172.20.0.2 6379
  ```
- `docker compose -p sentinel --file docker-compose.redis-sentinel.yml exec sentinel1 redis-cli -p 26379 sentinel masters`
  ```
  name
  mymaster
  ip
  172.20.0.2
  port
  6379
  flags
  master
  num-slaves
  1
  num-other-sentinels
  2
  quorum
  2
  ```
- `docker compose -p sentinel --file docker-compose.redis-sentinel.yml ps`
  ```
  NAME                       IMAGE     COMMAND                  SERVICE         CREATED         STATUS         PORTS
  redis-master               redis:8   "docker-entrypoint.s…"   redis-master    7 minutes ago   Up 7 minutes   6379/tcp
  sentinel-redis-replica-1   redis:8   "docker-entrypoint.s…"   redis-replica   7 minutes ago   Up 7 minutes   6379/tcp
  sentinel-sentinel1-1       redis:8   "docker-entrypoint.s…"   sentinel1       7 minutes ago   Up 7 minutes   6379/tcp, 0.0.0.0:26379->26379/tcp
  sentinel-sentinel2-1       redis:8   "docker-entrypoint.s…"   sentinel2       7 minutes ago   Up 7 minutes   6379/tcp, 0.0.0.0:26380->26379/tcp
  sentinel-sentinel3-1       redis:8   "docker-entrypoint.s…"   sentinel3       7 minutes ago   Up 7 minutes   6379/tcp, 0.0.0.0:26381->26379/tcp
  ```

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

### Phase 2 evidence (Django/Celery wiring on Apr 1, 2026)

- Environment and settings wiring:
  - `.env`
    ```dotenv
    CELERY_BROKER_URL="redis-sentinel://127.0.0.1:26379;127.0.0.1:26380;127.0.0.1:26381/0?service_name=mymaster"
    CELERY_RESULT_BACKEND="redis-sentinel://127.0.0.1:26379;127.0.0.1:26380;127.0.0.1:26381/1?service_name=mymaster"
    DJANGO_CACHE_URL="redis-sentinel://127.0.0.1:26379;127.0.0.1:26380;127.0.0.1:26381/2?service_name=mymaster"
    REDIS_URL="redis-sentinel://127.0.0.1:26379;127.0.0.1:26380;127.0.0.1:26381/2?service_name=mymaster"
    ```
  - `python -c "from config import settings; print(settings.CELERY_BROKER_URL); print(settings.CELERY_BROKER_TRANSPORT_OPTIONS); print(settings.CELERY_RESULT_BACKEND); print(settings.CELERY_RESULT_BACKEND_TRANSPORT_OPTIONS)"`
    ```
    sentinel://127.0.0.1:26379/0;sentinel://127.0.0.1:26380/0;sentinel://127.0.0.1:26381/0
    {'master_name': 'mymaster', 'sentinels': [['127.0.0.1', 26379], ['127.0.0.1', 26380], ['127.0.0.1', 26381]]}
    sentinel://127.0.0.1:26379/1;sentinel://127.0.0.1:26380/1;sentinel://127.0.0.1:26381/1
    {'master_name': 'mymaster', 'sentinels': [['127.0.0.1', 26379], ['127.0.0.1', 26380], ['127.0.0.1', 26381]]}
    ```
- Settings and parser verification:
  - `pytest tests/test_settings_redis_sentinel.py`
    ```
    collected 3 items
    tests/test_settings_redis_sentinel.py ...                                [100%]
    ============================== 3 passed in 0.18s ===============================
    ```
- Django cache verification:
  - `python manage.py shell -c "from django.core.cache import caches; cache = caches['default']; cache.set('phase2_probe', 'ok', 30); print(cache.get('phase2_probe'))"`
    ```
    ok
    ```
- Celery broker and backend verification:
  - `python -c "from config.celery import app; conn = app.connection(); conn.ensure_connection(max_retries=1); print(conn.transport.driver_name)"`
    ```
    redis
    ```
  - `python -c "from config.celery import app; backend = app.backend; print(type(backend).__name__); print(backend.client.ping())"`
    ```
    SentinelBackend
    True
    ```

### Phase 2 current status

- Django cache is using `django_redis.client.SentinelClient`.
- Celery broker and result backend are translated to `sentinel://...` URLs with `master_name=mymaster`.
- Fresh Django and Celery processes connect through Sentinel successfully.

## Phase 3 – Failover verification

1. While the stack is still running, stop the master container (`docker compose --file docker-compose.redis-sentinel.yml stop redis-master`).
2. Wait a few seconds for Sentinel to elect a new master and then run:
   ```bash
   docker compose --file docker-compose.redis-sentinel.yml exec sentinel1 redis-cli -p 26379 sentinel get-master-addr-by-name mymaster
   ```
   The host/port should now point to the promoted replica.
3. Execute a cache-dependent API call or Celery task during the failover; the request should succeed once the new master is ready. Celery logs may show reconnects but should not abort.
4. Monitor the Prometheus metrics `redis_sentinel_master_status`, `redis_sentinel_master_ok_sentinels`, and `redis_sentinel_master_ok_slaves` (if exporter is attached) to confirm visibility of the election.
5. Restart the original master so it rejoins as a replica, restoring the three-node topology.

### Phase 3 evidence (failover drill on Apr 1, 2026)

- Baseline before failover:
  - `docker compose -p sentinel --file docker-compose.redis-sentinel.yml exec sentinel1 redis-cli -p 26379 sentinel get-master-addr-by-name mymaster`
    ```
    172.20.0.2
    6379
    ```
  - `python manage.py shell -c "from django.core.cache import caches; cache = caches['default']; cache.set('phase3_baseline', 'before', 30); print(cache.get('phase3_baseline'))"`
    ```
    before
    ```
  - `python -c "from config.celery import debug_task; result = debug_task.delay(); print(result.id); print(result.get(timeout=20, propagate=False))"`
    ```
    3f1d24c1-dd16-469f-a7ed-7a4e5699da97
    None
    ```
- Failover trigger:
  - `docker compose -p sentinel --file docker-compose.redis-sentinel.yml stop redis-master`
    ```
    Container redis-master Stopped
    ```
  - First successful post-stop poll already showed the promoted replica:
    ```
    poll=1
    172.20.0.3
    6379
    ```
  - `docker compose -p sentinel --file docker-compose.redis-sentinel.yml logs sentinel1 --tail 60`
    ```
    sentinel1-1  | 1:X 01 Apr 2026 17:28:57.572 # +sdown master mymaster 172.20.0.2 6379
    sentinel1-1  | 1:X 01 Apr 2026 17:28:58.678 # +odown master mymaster 172.20.0.2 6379 #quorum 3/2
    sentinel1-1  | 1:X 01 Apr 2026 17:28:58.989 # +switch-master mymaster 172.20.0.2 6379 172.20.0.3 6379
    sentinel1-1  | 1:X 01 Apr 2026 17:28:59.017 * +slave slave 172.20.0.2:6379 172.20.0.2 6379 @ mymaster 172.20.0.3 6379
    ```
- Runtime behavior during failover:
  - Django cache stayed available:
    - `python manage.py shell -c "from django.core.cache import caches; cache = caches['default']; cache.set('phase3_failover', 'during', 30); print(cache.get('phase3_failover'))"`
      ```
      during
      ```
  - Celery accepted a task during failover, but the initial wait exceeded 30 seconds:
    - `python -c "from config.celery import debug_task; result = debug_task.delay(); print(result.id); print(result.get(timeout=30, propagate=False))"`
      ```
      d9c23bb8-0751-4816-a3b7-070fc1a26529
      celery.exceptions.TimeoutError: The operation timed out.
      ```
    - After recovery, the same task settled successfully:
      - `python -c "from celery.result import AsyncResult; from config.celery import app; result = AsyncResult('d9c23bb8-0751-4816-a3b7-070fc1a26529', app=app); print(result.state); print(result.ready())"`
        ```
        SUCCESS
        True
        ```
- Recovery after failover:
  - `docker compose -p sentinel --file docker-compose.redis-sentinel.yml start redis-master`
    ```
    Container redis-master Started
    ```
  - The promoted master remained stable on repeated polls:
    ```
    poll=2
    172.20.0.3
    6379
    ...
    poll=11
    172.20.0.3
    6379
    ```
  - The original master rejoined as a replica:
    - `docker compose -p sentinel --file docker-compose.redis-sentinel.yml exec sentinel1 redis-cli -p 26379 sentinel replicas mymaster`
      ```
      name
      172.20.0.2:6379
      flags
      slave
      master-link-status
      ok
      master-host
      172.20.0.3
      master-port
      6379
      ```
  - A fresh Celery task succeeded after recovery:
    - `python -c "from config.celery import debug_task; result = debug_task.delay(); print(result.id); print(result.get(timeout=20, propagate=False))"`
      ```
      8cf6b561-841e-480d-8432-12955019e71d
      None
      ```

### Phase 3 current status

- Sentinel election and topology recovery are verified.
- Django cache traffic is verified during failover.
- Celery recovered, but the task dispatched during failover did not complete within the initial 30 second wait; it reached `SUCCESS` only after recovery stabilized.
- The Prometheus checkpoint is verified through `redis_exporter` in Sentinel mode:
  - `redis_instance_info{job="redis_exporter"}` reports `redis_mode="sentinel"` and `tcp_port="26379"`.
  - `redis_sentinel_master_status{job="redis_exporter"}` reports the active `master_address` and `master_status="ok"`.
  - `redis_sentinel_master_ok_sentinels{job="redis_exporter"}` reports `3`.
  - `redis_sentinel_master_ok_slaves{job="redis_exporter"}` reports `1`.
- A second metrics-aware failover drill on Apr 1, 2026 switched the elected master from `172.20.0.3:6379` to `172.20.0.2:6379`, and Prometheus reflected the new `master_address` label after the next scrape.
- Celery task dispatch during that drill remained `PENDING` for about `54.7s` before returning `SUCCESS`; this is tolerable for background NDVI-style work but too slow for latency-sensitive queueing.

## Notes & references

- The sentinel stack aligns with the Phase 1 objective in `docs/architecture/ndvi-pipeline-evolution.md`, and the URL parsing that makes Django/Celery sentinel-aware lives in `config/settings.py`.
- You can reuse this compose file for automated smoke tests or CI targets, ensuring each phase (standup, configuration, failover) has a scripted verification step.
