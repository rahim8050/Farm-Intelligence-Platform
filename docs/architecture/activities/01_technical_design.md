# Technical Design Document: Activity Scheduling + Notification Engine

**Document Version:** 1.0  
**Date:** May 3, 2026  
**Status:** APPROVED FOR IMPLEMENTATION  
**System:** Activity Scheduling + Notification Engine

---

## 1. Architecture Overview

### 1.1 System Context

The Activity Scheduling Engine is an event-driven subsystem for managing time-based and event-triggered farm operations. It lives inside the Django + DRF backend and integrates with existing infrastructure.

```
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│                        ACTIVITY SCHEDULING ENGINE                          │
├───────────────────────────────────────────────────────────────────────────┬──────────────────┤
│                     API LAYER (Django + DRF)           │                  │
│  POST /api/v1/activities/  GET /api/v1/activities/     │                  │
│  POST /api/v1/activities/{id}/execute               │                  │
└───────────────────────────────────────────────────┴──────────────────┘
                                    │
                                    ▼
┌───────────────────────────────────────────────────────────────────────────┐
│                     DATABASE (PostgreSQL)                │
│                   Activity Model + Indexes               │
└───────────────────────────────────────────────────┘
                                    │
          ┌───────────────────────────┼───────────────────────────┐
          ▼                                               ▼
┌─────────────────────────────┐                 ┌─────────────────────────────┐
│  SCHEDULER LOOP          │                 │   WEBHOOK / EVENTS       │
│  (Celery Beat / cron)    │                 │   (Future NDVI)         │
│  Polls next_due_at      │                 │                        │
└─────────────────────────────┘                 └─────────────────────────────┘
          │                                               │
          ▼                                               ▼
┌───────────────────────────────────────────────────────────────────────────┐
│                       CELERY QUEUE (Redis)                         │
│              activity_dispatch (transient tasks)                    │
└─────────────────────────────────────────────────────────���─────────┬───────────┘
                                                                ▼
┌───────────────────────────────────────────────────────────────────────────┐
│                    WORKER EXECUTION LAYER                             │
│  ActivityHandlerRegistry → ActivityHandler.execute()                 │
│  → WebSocket emit via Django Channels                           │
└───────────────────────────────────────────────────────────────────────────┘
```

### 1.2 Component Responsibilities

| Component | Responsibility |
|-----------|-------------|
| API Layer | CRUD operations, validation, user isolation |
| Database | Persistence, query patterns, indexes |
| Scheduler Loop | Polls for due activities, dispatches to queue |
| Celery Queue | Transient task dispatch |
| Worker Execution | Handler invocation, notification delivery |
| Django Channels | Real-time WebSocket notifications |

### 1.3 Failure Boundaries

| Boundary | Behavior |
|----------|----------|
| API → DB | Database errors propagate to API (4xx/5xx) |
| Scheduler → Queue | Queue failures retry via Celery backoff |
| Worker → Handler | Handler failures go to retry or dead-letter |
| Worker → WebSocket | Connect failures logged, no blocking |

---

## 2. Sequence Diagrams

### 2.1 Activity Creation (POST)

```
User
  │
  ▼
┌──────────────────────┐
│   API Request     │
│ POST /activities/ │
└────────┬─────────┘
         │
         ▼
┌──────────────────────┐
│ Serializer       │
│ Validation      │
└────────┬─────────┘
         │ is_valid()
         ▼
┌──────────────────────┐
│ Activity.save()  │
│ compute next_due_at│
└────────┬─────────┘
         │
         ▼
┌──────────────────────┐
│ 201 Created      │
│ Response       │
└──────────────────────┘
```

### 2.2 Scheduler + Execution Flow

```
Scheduler
    │
    │ poll: next_due_at <= now AND status=PENDING
    ▼
┌──────────────────────┐
│ SELECT * FROM    │
│ activities      │
│ WHERE next...  │
└────────┬─────────┘
         │
         ▼ [batch]
┌──────────────────────┐
│ Celery task      │
│ dispatch       │
│ activity_dispatch│
└────────┬─────────┘
         │ apply_async
         ▼
┌──────────────────────┐
│ Worker           │
│ acquire lock     │
│ (Redis)         │
└────────┬─────────┘
         │
         ▼
┌──────────────────────┐
│ Lookup Handler  │
│ (Registry)    │
└────────┬─────────┘
         │
         ▼
┌──────────────────────┐
│ Handler.execute() │
│ (vaccination,     │
│  fertilizer...)  │
└────────┬─────────┘
         │
    ┌────┴────┐
    │ success │ failure
    ��        ▼
┌───────┐ ┌────────────────┐
│Update │ │ retry or fail  │
│status │ └────────────────┘
└──┬───┘
   │
   ▼
┌──────────────────────┐
│ WebSocket emit │
│ (Channels)   │
└─────────────┘
```

### 2.3 Recurrence Computation

```
Handler.execute() complete
    │
    ▼
┌──────────────────────┐
│ Check recurrence_type│
│ = interval       │
└────────┬─────────┘
         │
    ┌────┴────┐
    │ yes     │ no (one-time)
    ▼        ▼
┌──────────────────┐ ┌────────────────┐
│ compute new    │ │ COMPLETED     │
│ next_due_at   │ │ (terminal)   │
│ now + days   │ └─────────────┘
└──────┬──────┘
       │
       ▼
┌──────────────────────┐
│ Create new      │
│ Activity      │
│ (same type,   │
│  new due)    │
└──────────────┘
```

---

## 3. Activity Lifecycle Semantics

### 3.1 State Machine

```
                    ┌──────────┐
                    │ CREATED  │ (initial)
                    └────┬─────┘
                         │ scheduled_at <= now
                         ▼
                    ┌──────────┐
               ┌────►│ PENDING │◄──────┐
               │      └────┬─────┘           │
               │           │                 │
               │           │ dispatch        │ retry
               │           ▼                 │
               │      ┌──────────┐      │
               │      │DISPATCHED│      │
               │      └────┬─────┘      │
               │           │              │
               │           │ execute      │
               │           ▼              │
               │      ┌──────────┐      │
               │      │ RUNNING │      │
               │      └────┬─────┘      │
               │           │              │
               │     ┌─────┴─────┐       │
               │     │ success   │failure │
               │     ▼          ▼       │
               │  ┌──────┐  ┌─────┐  │
               │  │SUCCESS│  │FAILED│──┘
               │  └──┬───┘  └──┬──┘  │
               │     │          │      │
               │  recurrence   │      │
               │  (reschedule)│      │
               │     │          │      │
               │     ▼          ◄────┘
               │  ┌──────────┐
               └──│ PENDING │ (next occurrence)
                  └──────────┘
```

### 3.2 Status Definitions

| Status | Meaning |
|--------|---------|
| CREATED | Recently created, not yet due |
| PENDING | Due for execution |
| DISPATCHED | Claimed by scheduler, queued for worker |
| RUNNING | Currently being processed |
| SUCCESS | Completed successfully (terminal unless recurring) |
| FAILED | Execution failed after retries exhausted |
| RETRY | Scheduled for retry with backoff |

### 3.3 Activity Types

| Type | Handler | Recurrence Supported |
|------|---------|-----------------|
| vaccination | VaccinationHandler | interval |
| fertilizer | FertilizerHandler | interval |
| irrigation | IrrigationHandler | interval, conditional |
| ndvi_trigger | NdviTriggerHandler | event-based |

---

## 4. Scheduler Responsibilities

### 4.1 Scheduler Loop (Celery Beat Task)

```python
# activities/tasks.py
@app.task(name="activities.scheduler.poll")
def poll_activities():
    """Poll for due activities and dispatch to queue."""
    batch_size = settings.ACTIVITY_SCHEDULER_BATCH_SIZE
    
    due_activities = Activity.objects.filter(
        status=Activity.Status.PENDING,
        next_due_at__lte=timezone.now()
    ).select_related("owner")[:batch_size]
    
    dispatched = 0
    for activity in due_activities:
        try:
            # Try to claim via Redis lock
            if acquire_activity_lock(activity.id):
                dispatch_activity.delay(activity.id)
                dispatched += 1
        except Exception as e:
            logger.warning("Failed to dispatch activity %d: %s", activity.id, e)
    
    return {"dispatched": dispatched, "scanned": len(due_activities)}
```

### 4.2 Scheduling Constraints

| Constraint | Value |
|------------|-------|
| Poll interval | 60 seconds (Celery Beat) |
| Batch size | 100 |
| Lock TTL | 300 seconds |
| Max concurrent per worker | 10 |

### 4.3 Scheduler Rules

- **Never** execute activity directly (always dispatch)
- **Always** use Redis lock before dispatch
- **Never** block on lock acquisition (skip if lock held)
- **Always** update status to RUNNING before dispatch
- **Never** reschedule inside scheduler

---

## 5. Worker Responsibilities

### 5.1 Dispatch Task

```python
# activities/tasks.py
@app.task(
    name="activities.dispatch",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
)
def dispatch_activity(self, activity_id: int):
    """Execute activity via handler."""
    try:
        activity = Activity.objects.get(id=activity_id)
    except Activity.DoesNotExist:
        return {"status": "not_found"}
    
    # Check idempotency
    if activity.status == Activity.Status.RUNNING:
        # Check if stale (lock expired)
        if is_activity_lock_stale(activity_id):
            activity.status = Activity.Status.PENDING
            activity.save(update_fields=["status"])
        else:
            return {"status": "already_running"}
    
    # Update status
    activity.status = Activity.Status.RUNNING
    activity.save(update_fields=["status"])
    
    # Get handler
    handler = get_handler(activity.type)
    
    try:
        result = handler.execute(activity)
        handle_success(activity, result)
        return {"status": "success", "result": result}
    except Exception as e:
        handle_failure(activity, e)
        raise self.retry(exc=e)
```

### 5.2 Worker Rules

- **Always** update status before execution
- **Always** release lock on completion/failure
- **Never** raise from handler (catch and update status)
- **Always** emit WebSocket on completion
- **Always** compute recurrence on success (if applicable)

---

## 6. Locking/Idempotency Strategy

### 6.1 Distributed Lock (Redis)

```python
# activities/locks.py
import redis
import json
import uuid

LOCK_PREFIX = "activity:lock:"
LOCK_TTL = 300  # 5 minutes


def acquire_activity_lock(activity_id: int, worker_id: str = None) -> bool:
    """Acquire lock for activity execution."""
    if worker_id is None:
        worker_id = uuid.uuid4().hex
    
    lock_key = f"{LOCK_PREFIX}{activity_id}"
    
    #原子 compare-and-set
    acquired = redis_client.set(
        lock_key,
        worker_id,
        nx=True,  # 只有键不存在才设置
        ex=LOCK_TTL
    )
    
    return bool(acquired)


def release_activity_lock(activity_id: int, worker_id: str) -> bool:
    """Release lock only if owned by worker."""
    lock_key = f"{LOCK_PREFIX}{activity_id}"
    
    # Lua script for atomic check-and-delete
    lua_script = """
    if redis.call("get", KEYS[1]) == ARGV[1] then
        return redis.call("del", KEYS[1])
    else
        return 0
    end
    """
    
    result = redis_client.eval(lua_script, 1, lock_key, worker_id)
    return bool(result)


def is_activity_lock_stale(activity_id: int) -> bool:
    """Check if lock has expired."""
    lock_key = f"{LOCK_PREFIX}{activity_id}"
    return redis_client.get(lock_key) is None
```

### 6.2 Idempotency Rules

| Scenario | Strategy |
|----------|----------|
| Double dispatch | Redis lock prevents |
| Worker crash | Lock TTL auto-releases |
| Retry after failure | Status check before retry |
| Same activity re-run | Check last_executed_at in handler |

### 6.3 Lock Flow

```
Scheduler detects due activity
    │
    ▼
acquire_activity_lock(id, worker_id)
    │
    │ True    │ False (another worker has lock)
    ▼        ▼
Dispatch   Skip
```

---

## 7. Retry Policy

### 7.1 Retry Configuration

| Setting | Value |
|---------|-------|
| Max retries | 3 |
| Base delay | 60 seconds |
| Backoff multiplier | 2x |
| Max delay | 600 seconds |

### 7.2 Retry Decision Matrix

| Error Type | Retry? | Next Action |
|----------|-------|----------|
| HandlerException.Temporary | Yes | Exponential backoff |
| HandlerException.Permanent | No | Move to FAILED |
| Redis ConnectionError | Yes | Exponential backoff |
| Database IntegrityError | No | Move to FAILED |
| Lock held by other | No | Skip, reschedule |

### 7.3 Retry Implementation

```python
# activities/tasks.py
class ActivityDispatchTask(Task):
    autoretry_for = (RedisError, ConnectionError)
    retry_backoff = True
    retry_backoff_max = 600
    retry_jitter = True
    
    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """Handle final failure."""
        activity_id = args[0]
        
        if isinstance(exc, HandlerException.Permanent):
            Activity.objects.filter(id=activity_id).update(
                status=Activity.Status.FAILED,
                last_error=str(exc)
            )
        else:
            # Will retry
            logger.warning("Activity %d failed: %s", activity_id, exc)
```

---

## 8. WebSocket Event Schema

### 8.1 Django Channels Setup

```python
# config/asgi.py
import os
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

application = ProtocolTypeRouter({
    "http": URLRouter([
        # ...existing routes...
    ]),
    "websocket": AuthMiddlewareStack(
        URLRouter([
            # WebSocket URL patterns
            re_path(r"ws/activities/$", ActivityConsumer.as_asgi()),
        ])
    ),
})
```

### 8.2 Consumer Implementation

```python
# activities/consumers.py
import json
from channels.generic.websocket import AsyncWebsocketConsumer


class ActivityConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user = self.scope["user"]
        
        if not self.user.is_authenticated:
            await self.close()
            return
        
        # Join user-specific group
        self.group_name = f"user_{self.user.id}"
        await self.channel_layer.group_add(
            self.group_name,
            self.channel_name
        )
        
        await self.accept()
    
    async def disconnect(self(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(
                self.group_name,
                self.channel_name
            )
    
    async def activity_event(self, event):
        """Handle activity events from worker."""
        await self.send(text_data=json.dumps({
            "type": "activity_event",
            "event": event["event"],
        }))
```

### 8.3 Event Payload Schema

```python
{
    "type": "activity_event",
    "event": {
        "activity_id": 123,
        "activity_type": "fertilizer",
        "action": "completed",  # created, started, completed, failed
        "farm_id": 456,
        "message": "Fertilizer applied",
        "timestamp": "2026-05-03T12:00:00Z",
        "metadata": {
            "amount_kg": 50,
            "fertilizer_type": "urea"
        }
    }
}
```

### 8.4 Emitter Implementation

```python
# activities/emitters.py
from channels.layers import get_channel_layer


async def emit_activity_event(user_id: int, event: dict):
    """Emit activity event to user's WebSocket."""
    channel_layer = get_channel_layer()
    
    await channel_layer.group_send(
        f"user_{user_id}",
        {
            "type": "activity_event",
            "event": event,
        }
    )
```

---

## 9. Database Schema

### 9.1 Activity Model

```python
# activities/models.py
from django.db import models
from django.conf import settings


class Activity(models.Model):
    """Activity scheduler model."""
    
    class Type(models.TextChoices):
        VACCINATION = "vaccination", "Vaccination"
        FERTILIZER = "fertilizer", "Fertilizer"
        IRRIGATION = "irrigation", "Irrigation"
        NDVI_TRIGGER = "ndvi_trigger", "NDVI Trigger"
    
    class Status(models.TextChoices):
        CREATED = "created", "Created"
        PENDING = "pending", "Pending"
        DISPATCHED = "dispatched", "Dispatched"
        RUNNING = "running", "Running"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"
        RETRY = "retry", "Retry"
    
    class RecurrenceType(models.TextChoices):
        NONE = "none", "One-time"
        INTERVAL = "interval", "Interval"
        CRON = "cron", "Cron (future)"
    
    # Relations
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="activities"
    )
    farm = models.ForeignKey(
        "farms.Farm",
        on_delete=models.CASCADE,
        related_name="activities",
        null=True,
        blank=True,
    )
    
    # Core fields
    type = models.CharField(max_length=50, choices=Type.choices)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.CREATED
    )
    
    # Scheduling
    scheduled_at = models.DateTimeField()
    next_due_at = models.DateTimeField(db_index=True)
    last_executed_at = models.DateTimeField(null=True, blank=True)
    
    # Recurrence
    recurrence_type = models.CharField(
        max_length=20,
        choices=RecurrenceType.choices,
        default=RecurrenceType.NONE
    )
    interval_days = models.PositiveIntegerField(null=True, blank=True)
    
    # Metadata (type-specific)
    metadata = models.JSONField(default=dict, blank=True)
    
    # Execution tracking (per prompts/harden.md)
    execution_id = models.UUIDField(null=True, blank=True, editable=False)
    execution_started_at = models.DateTimeField(null=True, blank=True)
    execution_completed_at = models.DateTimeField(null=True, blank=True)
    
    # Retry configuration
    retry_count = models.PositiveIntegerField(default=0)
    max_retries = models.PositiveIntegerField(default=3)
    
    # Error tracking
    last_error = models.TextField(null=True, blank=True)
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ["next_due_at"]
        indexes = [
            # For scheduler polling
            models.Index(
                fields=["status", "next_due_at"]
            ),
            # For user queries
            models.Index(
                fields=["owner", "status", "next_due_at"]
            ),
            # For farm queries
            models.Index(
                fields=["farm", "status", "next_due_at"]
            ),
        ]
    
    def save(self, *args, **kwargs):
        # Compute next_due_at for interval recurrence
        if self.recurrence_type == self.RecurrenceType.INTERVAL and self.interval_days:
            if self.next_due_at is None:
                from django.utils import timezone
                from datetime import timedelta
                self.next_due_at = self.scheduled_at + timedelta(days=self.interval_days)
        
        super().save(*args, **kwargs)
```

### 9.2 Index Usage Patterns

| Query | Index |
|-------|-------|
| `status=PENDING AND next_due_at <= now` | (status, next_due_at) |
| `owner=id AND status=x` | (owner, status, next_due_at) |
| `farm=id AND status=x` | (farm, status, next_due_at) |

---

## 10. Observability Strategy

### 10.1 Structured Logging

```python
import logging

logger = logging.getLogger("activities")


def log_activity_event(level: str, activity: Activity, action: str, **kwargs):
    """Structured log with context."""
    logger.log(
        getattr(logging, level),
        "activity_event: activity_id=%d type=%s action=%s owner_id=%d farm_id=%s %s",
        activity.id,
        activity.type,
        action,
        activity.owner_id,
        activity.farm_id if activity.farm_id else "none",
        " ".join(f"{k}={v}" for k, v in kwargs.items())
    )
```

### 10.2 Log Actions

| Action | Level | Fields |
|-------|-------|-------|
| created | INFO | activity_id, type, owner_id, farm_id |
| dispatched | INFO | activity_id, type, worker_id |
| started | INFO | activity_id, handler |
| completed | INFO | activity_id, duration_ms |
| failed | ERROR | activity_id, error, retry_count |
| retried | WARNING | activity_id, retry_count, next_retry |

### 10.3 Metrics

```python
# activities/metrics.py
from prometheus_client import Counter, Histogram, Gauge

activities_dispatched = Counter(
    "activities_dispatched_total",
    "Activities dispatched",
    ["type", "status"]
)

activity_duration_seconds = Histogram(
    "activity_duration_seconds",
    "Activity execution duration",
    ["type"]
)

activities_active = Gauge(
    "activities_active",
    "Currently active activities",
    ["type", "status"]
)
```

### 10.4 Tracing Fields

| Field | Source |
|-------|--------|
| activity_id | DB |
| activity_type | DB |
| owner_id | DB |
| farm_id | DB |
| handler | Registry |
| worker_id | Redis lock |
| duration_ms | Worker |

---

## 10B. Cache Strategy for Farm State

This section documents cache correctness and efficiency for farm state caching. This applies to the `/api/v1/farm-state/{farm_id}` endpoint and related caching layers.

### 10B.1 The Cache Stampede Problem

**What is a cache stampede?**

Also known as the "dogpile problem", a cache stampede occurs when:

1. Cache entry expires
2. Multiple concurrent requests see the expired/missing cache
3. All requests bypass the cache and hit the backend simultaneously
4. Backend is flooded with duplicate computation requests
5. All requests compute and populate the cache with the same result

**Why it matters for farm state:**

- NDVI computation is expensive (upstream API calls, raster processing)
- Multiple users may query the same farm simultaneously
- Expired cache = thundering herd on backend services

### 10B.2 Cache Stampede Protection Strategy

**Approach: Lightweight mutex using Django cache**

```python
# ndvi/cache.py
from django.core.cache import cache
import functools
import logging
import hashlib

logger = logging.getLogger("ndvi")

# Cache lock configuration
CACHE_LOCK_TTL = 30  # seconds - must exceed typical compute time
CACHE_DEFAULT_TTL = 21600  # 6 hours
CACHE_LOCK_PREFIX = "ndvi:lock:"


def cached_with_stampede_protection(cache_key: str, ttl: int = None):
    """
    Decorator-style cache with stampede protection.

    Flow:
    1. First lookup: cache.get(key)
    2. If miss: try to acquire lock
    3. If lock acquired: compute and cache
    4. If lock not acquired: return stale or wait briefly
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return _cache_get_or_compute(
                cache_key,
                lambda: func(*args, **kwargs),
                ttl or CACHE_DEFAULT_TTL
            )
        return wrapper
    return decorator


def _cache_get_or_compute(cache_key: str, compute_fn, ttl: int):
    """
    Get from cache or compute with stampede protection.

    Returns: cached_value or computed_value
    """
    lock_key = f"{CACHE_LOCK_PREFIX}{hashlib.md5(cache_key.encode()).hexdigest()}"

    # Step 1: First cache lookup
    cached = cache.get(cache_key)
    if cached is not None:
        logger.debug("cache_hit: key=%s", cache_key)
        return cached

    logger.debug("cache_miss: key=%s", cache_key)

    # Step 2: Try to acquire lock
    lock_acquired = cache.add(lock_key, "1", CACHE_LOCK_TTL)

    if lock_acquired:
        # Step 3: We own the lock - compute and cache
        try:
            logger.info("lock_acquired: key=%s", cache_key)
            result = compute_fn()

            # Cache the computed result
            cache.set(cache_key, result, ttl)
            logger.info("cache_populated: key=%s ttl=%d", cache_key, ttl)

            return result
        finally:
            # Step 4: Always release lock in finally
            try:
                cache.delete(lock_key)
                logger.debug("lock_released: key=%s", cache_key)
            except Exception as e:
                logger.warning("lock_release_failed: key=%s error=%s", cache_key, e)
    else:
        # Step 5: Another process has the lock
        # Wait briefly then retry cache
        logger.debug("lock_contention: key=%s waiting...", cache_key)

        # Brief wait (non-blocking)
        import time
        time.sleep(0.5)  # 500ms max wait

        # Retry cache lookup
        cached = cache.get(cache_key)
        if cached is not None:
            logger.info("cache_hit_after_wait: key=%s", cache_key)
            return cached

        # Still no cache - return stale fallback or recompute
        logger.warning("stale_fallback: key=%s", cache_key)
        return compute_fn()
```

### 10B.3 Cache Lock Flow

```
User Request (cache miss)
    │
    ▼
cache.add(lock_key, "1", TTL)
    │
    │ True    │ False (another has lock)
    ▼        ▼
Compute   Wait 500ms
    │        │
    │        ▼
cache.set(cache_key, result)   Retry cache.get()
    │        │              │
    │        │        cached │ None
    │        │        ▼      ▼
    │        │    Return  Wait again or
    │        │    cached  stale fallback
cache.delete(lock_key)
    │
    ▼
Return result
```

### 10B.4 Constraints (CRITICAL)

| Constraint | Rule |
|------------|-------|
| No blocking | Never block waiting for lock > 2 seconds |
| Lock owner only | Only lock owner may compute cache value |
| Always release | Lock must be released in `finally:` block |
| Auto-expiry | Lock TTL auto-expires - prevents deadlocks |
| No Celery dispatch | GET endpoints never dispatch Celery tasks |
| Django cache API | Use `django.core.cache`, not raw Redis |
| Response unchanged | API response format unchanged |

### 10B.5 Cache TTL Strategy

**Updated Recommendation: 6 hours**

| Aspect | Value | Rationale |
|--------|-------|-----------|
| TTL | 21600 seconds (6 hours) | NDVI data updates daily at most |
| Jitter | ±10% random | Avoids synchronized expiry waves |

**TTL Jitter Implementation:**

```python
import random

def compute_cache_ttl(base_ttl: int, jitter_pct: float = 0.10) -> int:
    """Add random jitter to prevent synchronized expiry."""
    jitter = base_ttl * jitter_pct
    return int(base_ttl + random.uniform(-jitter, jitter))
```

**Rationale:**

- NDVI satellite data refreshes daily at most
- Farm state computation is expensive
- 6-hour TTL reduces backend load by ~75% vs 1-hour
- Better cache hit rates for repeated queries

### 10B.6 Failure Modes

| Scenario | Behavior |
|----------|----------|
| Lock expires during compute | Another request may start compute; deduplication via request_hash |
| Cache backend outage | Fallback to direct computation; log error |
| Simultaneous recompute race | First-compute-wins; others wait briefly then retry |
| Stale-but-safe fallback | Return cached value (allow slight staleness) |

### 10B.7 Stale-But-Safe Fallback

```python
def get_with_fallback(cache_key: str, compute_fn, max_stale_seconds: int = 3600):
    """
    Get cached value with stale-allowance.

    If cache is down:
    - Allow returning slightly stale data
    - Log the staleness
    """
    try:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
    except Exception as e:
        logger.warning("cache_backend_error: key=%s error=%s", cache_key, e)
        # Fallback: compute directly (bypass cache)
        return compute_fn()

    # If cache miss, use compute WITH stampede protection
    return _cache_get_or_compute(cache_key, compute_fn, CACHE_DEFAULT_TTL)
```

### 10B.8 Observability

**Cache-Specific Metrics:**

```python
# ndvi/metrics.py
from prometheus_client import Counter, Histogram, Gauge

cache_hit = Counter(
    "ndvi_cache_hit_total",
    "Cache hits",
    ["endpoint"]
)

cache_miss = Counter(
    "ndvi_cache_miss_total",
    "Cache misses",
    ["endpoint"]
)

lock_acquired = Counter(
    "ndvi_lock_acquired_total",
    "Cache locks acquired",
    ["result"]  # success, contention
)

lock_contention = Counter(
    "ndvi_lock_contention_total",
    "Lock contention events"
)

recompute_duration_seconds = Histogram(
    "ndvi_recompute_duration_seconds",
    "Time to recompute cache",
    ["endpoint"]
)
```

**Logging Fields:**

| Field | Description |
|-------|-------------|
| cache_key | Full cache key |
| hit_miss | "hit" or "miss" |
| lock_result | "acquired", "waited", "fallback" |
| compute_duration_ms | Time to compute |
| stale_seconds | Seconds since cache write |

### 10B.9 Validation Requirements

**Test Scenarios:**

| Scenario | Validation |
|----------|------------|
| Concurrent expiry | 10 simultaneous requests: only 1 compute |
| Lock contention | 5 requests: 1 acquires, 4 wait and hit |
| Cache hit rate | Hit rate > 80% after warm-up |
| Degraded fallback | Cache down: direct compute succeeds |

---

## 11. Scaling Strategy

### 11.1 Horizontal Worker Scaling

```
                    ┌──────────────────┐
                    │  Load Balancer   │
                    └────────┬─────────┘
                             │
        ┌─────────────────────┼─────────────────────┐
        │                    │                    │
        ▼                    ▼                    ▼
   ┌─────────┐         ┌─────────┐         ┌─────────┐
   │Worker 1 │         │Worker 2 │         │Worker 3 │
   │Celery   │         │Celery   │         │Celery   │
   └─────────┘         └─────────┘         └─────────┘
        │                    │                    │
        └────────────────────┼────────────────────┘
                           ▼
                    ┌──────────────┐
                    │   Redis     │
                    │   Locks    │
                    └──────────────┘
```

### 11.2 Scheduler Scaling

- Single Celery Beat scheduler (leader)
- Scheduler runs every 60 seconds
- Each worker polls independently via Redis locks
- No coordination needed beyond lock

### 11.3 Database Scaling

| Technique | Implementation |
|-----------|-----------------|
| Read replicas | DRF read replicas |
| Connection pooling | pgbouncer (future) |
| Partitioning | By owner_id (future) |

### 11.4 Redis Scaling

| Resource | Current | Scaling |
|----------|---------|---------|
| Celery connections | Pooled | Add replicas |
| Lock Redis | Single | Sentinel (existing) |
| Channels Redis | Single | Sentinel (existing) |

### 11.5 Scaling Constraints

- **Max concurrent activities per worker:** 10
- **Scheduler poll interval:** 60s
- **Lock TTL:** 300s
- **Batch size:** 100

---

## 12. Failure Modes

### 12.1 Failure Matrix

| Component | Failure | Impact | Recovery |
|-----------|---------|--------|----------|
| Scheduler | Redis down | No new dispatches | Retry on next poll |
| Worker |Handler crash | Activity stuck RUNNING | Lock expires, reschedule |
| Database | Connection error | All operations fail | Celery retry |
| WebSocket | Channel disconnect | No notifications | Log and continue |
| Lock | Redis restart | Stale locks | TTL auto-cleanup |

### 12.2 Dead Activities

```python
# Detection: status=RUNNING for > 30 minutes
STALE_THRESHOLD = timedelta(minutes=30)

# Recovery task
def recover_stale_activities():
    stale = Activity.objects.filter(
        status=Activity.Status.RUNNING,
        updated_at__lt=timezone.now() - STALE_THRESHOLD
    )
    
    for activity in stale:
        # Release lock (will expire anyway)
        release_activity_lock(activity.id, "*")
        
        # Reset status
        activity.status = Activity.Status.PENDING
        activity.retry_count += 1
        activity.save()
```

### 12.3 Circuit Breaker (Future)

For handler-specific failures:

| Consecutive Failures | Action |
|---------------------|--------|
| 5 | Pause handler for 5 minutes |
| 10 | Disable handler, alert |
| 20 | Disable type, alert |

---

## 13. Phased Implementation Plan

### Phase 1: Core API (Week 1-2)

**Objective:** CRUD without scheduling

- [ ] Activity model with migrations
- [ ] POST /api/v1/activities/ endpoint
- [ ] GET /api/v1/activities/ endpoint
- [ ] GET /api/v1/activities/{id}/ endpoint
- [ ] PATCH /api/v1/activities/{id}/ endpoint
- [ ] DELETE /api/v1/activities/{id}/ endpoint
- [ ] Serializer validation
- [ ] User isolation (queryset filtering)
- [ ] Unit tests for API

### Phase 2: Scheduler (Week 3-4)

**Objective:** Basic scheduling

- [x] Activity model with migrations
- [x] POST /api/v1/activities/ endpoint
- [x] GET /api/v1/activities/ endpoint
- [x] GET /api/v1/activities/{id}/ endpoint
- [x] PATCH /api/v1/activities/{id}/ endpoint
- [x] DELETE /api/v1/activities/{id}/ endpoint
- [x] Serializer validation
- [x] User isolation (queryset filtering)
- [x] Unit tests for API
- [x] Service layer (atomic claim, state machine)
- [x] Execution model (execution_id, DISPATCHED state)
- [x] Handler registry stub
- [x] Scheduler task (poll_activities)
- [x] Execute task (execute_activity)
- [x] Recovery task (recover_stale)

### Phase 3: Execution + WebSocket (Week 5-6)

**Objective:** Handler execution + notifications

- [ ] Django Channels setup
- [ ] WebSocket consumer
- [ ] Activity handlers (vaccination, fertilizer, irrigation)
- [ ] WebSocket event emitter
- [ ] Structured logging
- [ ] Metrics
- [ ] End-to-end tests

### Phase 4: NDVI Integration (Week 7-8)

**Objective:** Event-driven triggers

- [ ] NDVI event listener
- [ ] ndvi_trigger handler
- [ ] Conditional recurrence
- [ ] Activity chaining
- [ ] Advanced recurrence
- [ ] Performance testing

### Phase 5: Hardening (Week 9+)

**Objective:** Production-ready

- [ ] Circuit breaker
- [ ] Dead letter handling
- [ ] Load testing
- [ ] Documentation
- [ ] Runbook

---

## Appendix A: Activity Handlers

### Handler Interface

```python
# activities/handlers/base.py
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class HandlerResult:
    success: bool
    message: str
    metadata: dict = None


class ActivityHandler(ABC):
    type: str
    
    @abstractmethod
    def execute(self, activity: "Activity") -> HandlerResult:
        pass
    
    def validate(self, activity: "Activity") -> bool:
        return True
```

### Example Handlers

```python
# activities/handlers/vaccination.py
class VaccinationHandler(ActivityHandler):
    type = "vaccination"
    
    def execute(self, activity: Activity) -> HandlerResult:
        # Vaccination logic
        return HandlerResult(
            success=True,
            message="Vaccination completed",
            metadata={"cattle_id": activity.metadata.get("cattle_id")}
        )


# activities/handlers/fertilizer.py
class FertilizerHandler(ActivityHandler):
    type = "fertilizer"
    
    def execute(self, activity: Activity) -> HandlerResult:
        # Fertilizer application logic
        return HandlerResult(
            success=True,
            message="Fertilizer applied",
            metadata={"amount_kg": activity.metadata.get("amount_kg")}
        )
```

### Handler Registry

```python
# activities/handlers/registry.py
from activities.handlers.base import ActivityHandler


HANDLER_REGISTRY: dict[str, type[ActivityHandler]] = {}


def register_handler(handler_class: type[ActivityHandler]):
    HANDLER_REGISTRY[handler_class.type] = handler_class
    return handler_class


def get_handler(activity_type: str) -> ActivityHandler:
    handler_class = HANDLER_REGISTRY.get(activity_type)
    if handler_class is None:
        raise ValueError(f"No handler for type: {activity_type}")
    return handler_class()
```

---

## Appendix B: API Serializers

```python
# activities/serializers.py
from rest_framework import serializers
from activities.models import Activity


class ActivitySerializer(serializers.ModelSerializer):
    class Meta:
        model = Activity
        fields = [
            "id", "type", "status",
            "scheduled_at", "next_due_at", "last_executed_at",
            "recurrence_type", "interval_days",
            "farm", "metadata",
            "created_at", "updated_at"
        ]
        read_only_fields = [
            "id", "status", "next_due_at",
            "last_executed_at", "created_at", "updated_at"
        ]


class ActivityCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Activity
        fields = [
            "type", "scheduled_at",
            "recurrence_type", "interval_days",
            "farm", "metadata"
        ]
    
    def validate(self, attrs):
        if attrs.get("recurrence_type") == Activity.RecurrenceType.INTERVAL:
            if not attrs.get("interval_days"):
                raise serializers.ValidationError(
                    "interval_days required for interval recurrence"
                )
        return attrs
```

---

## Appendix C: API Views

```python
# activities/views.py
from rest_framework import viewsets, status
from rest_framework.response import Response
from config.api.responses import success_response


class ActivityViewSet(viewsets.ModelViewSet):
    """Activity CRUD operations.
    
    Auth: IsAuthenticated
    """
    
    serializer_class = ActivitySerializer
    
    def get_queryset(self):
        return Activity.objects.filter(owner=self.request.user)
    
    def create(self, request, *args, **kwargs):
        serializer = ActivityCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        activity = serializer.save(owner=request.user)
        
        return success_response(
            ActivitySerializer(activity).data,
            status_code=status.HTTP_201_CREATED
        )
```

---

## Appendix D: Settings

```python
# config/settings.py

# Activity Scheduling
ACTIVITY_SCHEDULER_BATCH_SIZE = 100
ACTIVITY_SCHEDULER_POLL_INTERVAL = 60
ACTIVITY_LOCK_TTL = 300
ACTIVITY_MAX_WORKER_CONCURRENT = 10
ACTIVITY_MAX_RETRIES = 3
ACTIVITY_RETRY_DELAY = 60
ACTIVITY_RETRY_BACKOFF_MAX = 600
```

---

## Document Control

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | May 3, 2026 | opencode | Initial TDD |
| 1.1 | May 3, 2026 | opencode | Added cache strategy (Section 10B) for farm state caching |
| 1.2 | May 5, 2026 | opencode | execution_id lifecycle, DISPATCHED state, atomic claim per prompts/harden.md |

---

**END OF TECHNICAL DESIGN DOCUMENT**