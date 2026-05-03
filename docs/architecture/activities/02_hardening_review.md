# Production Hardening Review: Activity Scheduling Engine

**Review Date:** May 3, 2026  
**Reviewer:** opencode  
**System:** Activity Scheduling + Notification Engine  
**TDD Version:** 1.0

---

## Executive Summary

This review identifies **critical hardening issues** in the TDD that must be addressed before production deployment. The original TDD is missing several distributed systems safeguards that will cause failures in production.

**OVERALL RISK: HIGH** - Multiple critical gaps identified.

---

## 1. Concurrency Correctness

### Issue 1.1: Lock Acquired in Scheduler, Released in Worker

**CRITICAL** - The TDD acquires the lock in the scheduler but releases it in the worker. This is a **split-brain problem**.

```
Scheduler: acquire_activity_lock(id) → dispatch task
Worker: do work → release_activity_lock(id)
```

**Problem:** If the Celery task succeeds but worker dies before release, lock is held for TTL (5 min). If lock expires during execution, another worker can claim and re-execute.

**Impact:** Duplicate execution possible.

**Fix Required:** Use **single-phase locking** - either:
- **Option A:** Acquire AND release in scheduler (stateless worker)
- **Option B:** Acquire AND release in worker (ignore scheduler lock)

### Issue 1.2: Check-Then-Act Race in Worker

**MEDIUM** - The worker does:
```python
if activity.status == RUNNING:
    if is_activity_lock_stale(id):
        activity.status = PENDING  # RACE WINDOW
        activity.save()           # Another worker could read PENDING here
    else:
        return "already_running"
```

**Problem:** Between check and save, another worker can read PENDING and also start.

**Fix Required:** Use **atomic update with status check**:
```python
updated = Activity.objects.filter(
    id=activity_id,
    status=Activity.Status.PENDING
).update(status=Activity.Status.RUNNING)

if not updated:
    return {"status": " contention}
```

### Issue 1.3: Multiple Scheduler Instances

**MEDIUM** - If multiple Celery Beat instances run, they will poll the same activities.

**Fix Required:** Add **scheduler leader election** using Redis:
```python
# Only one scheduler should poll
SCHEDULER_LOCK = "activities:scheduler:lock"

def is_scheduler_leader():
    return redis_client.set(SCHEDULER_LOCK, hostname, nx=True, ex=60)
```

---

## 2. Stale Execution Recovery

### Issue 2.1: Timeout Not Enforced

**CRITICAL** - The TDD has no execution timeout. A handler that hangs will leave activity in RUNNING forever.

**Fix Required:** Add **timeout enforcement**:
```python
# Celery task timeout
@app.task(bind=True, time_limit=300, soft_time_limit=270)
def dispatch_activity(self, activity_id: int):
    # If exceeds 5 minutes, task is killed
    # Must have separate recovery mechanism
```

### Issue 2.2: Recovery Task Missing

**MEDIUM** - Section 12.2 mentions stale recovery but doesn't schedule it.

**Fix Required:** Add Celery Beat schedule:
```python
# Add to Celery Beat schedule
CELERY_BEAT_SCHEDULE = {
    "recover-stale-activities": {
        "task": "activities.recover_stale",
        "schedule": 300.0,  # Every 5 minutes
    },
}
```

### Issue 2.3: Lost Activity Detection

**MEDIUM** - `updated_at` only tracks DB update, not execution start.

**Fix Required:** Add **execution_started_at** field:
```python
execution_started_at = models.DateTimeField(null=True, blank=True)

# Recovery check
STALE_THRESHOLD = timedelta(minutes=5)  # Much shorter!
```

---

## 3. Notification Durability

### Issue 3.1: Fire-and-Forget WebSocket

**CRITICAL** - The TDD emits WebSocket events but doesn't guarantee delivery.

```python
async def emit_activity_event(user_id: int, event: dict):
    # If WebSocket disconnected, event is LOST
    await channel_layer.group_send(...)
```

**Problem:** User offline = notification lost forever.

**Fix Required:** Implement **notification store-and-forward**:
```python
# Store in DB
class ActivityNotification(models.Model):
    user_id = models.IntegerField()
    activity_id = models.IntegerField()
    payload = models.JSONField()
    delivered = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

# On connect, deliver pending
# On ack, mark delivered
```

### Issue 3.2: No Acknowledgment Protocol

**MEDIUM** - No way to know if client received notification.

**Fix Required:** Add **message acknowledged** field:
```python
{
    "type": "activity_event",
    "event": {...},
    "message_id": "uuid",
    "ack_required": True
}
```

---

## 4. DB Growth Strategy

### Issue 4.1: No Cleanup for DONE Activities

**HIGH** - DONE activities accumulate forever.

**Fix Required:** Add **auto-cleanup**:
```python
# Celery Beat task to archive old activities
class ActivityArchiveTask:
    run_every = 86400  # Daily
    
    def run(self):
        # Archive DONE activities older than 90 days
        Activity.objects.filter(
            status=Activity.Status.DONE,
            updated_at__lt=now() - timedelta(days=90)
        ).update(archived=True)
```

### Issue 4.2: No Pagination in Schedule Query

**MEDIUM** - The scheduler query has no ORDER BY or LIMIT guarantees.

**Fix Required:** Add deterministic ordering:
```python
due_activities = Activity.objects.filter(
    status=Activity.Status.PENDING,
    next_due_at__lte=timezone.now()
).order_by("next_due_at", "id")[:batch_size]
```

### Issue 4.3: Recurring Activities Grow Unbounded

**MEDIUM** - Daily recurring activity = 365 rows/year.

**Fix Required:** Consider **activity consolidation**:
```python
# Keep only next N occurrences
# Or use virtual recurrence (compute on read)
```

---

## 5. WebSocket Auth

### Issue 5.1: AuthMiddlewareStack Misuse

**CRITICAL** - The TDD uses `AuthMiddlewareStack` but doesn't enforce JWT.

**Problem:** `scope["user"]` comes from sessions, not JWT.

**Fix Required:** Implement **JWT WebSocket auth**:
```python
# activities/middleware.py
class JWTAuthMiddleware:
    async def __init__(self, app):
        self.app = app
    
    async def __call__(self, scope, receive, send):
        # Extract token from query string
        query = dict.parse_qs(scope.get("query_string", b""))
        token = query.get(b"token", [b""])[0]
        
        # Validate JWT
        user = await validate_jwt(token)
        if not user:
            await self.close_connection(send, 401)
            return
        
        scope["user"] = user
        return await self.app(scope, receive, send)
```

### Issue 5.2: No Subscription Validation

**MEDIUM** - User can subscribe to any user's activities.

```python
self.group_name = f"user_{self.user.id}"  # Only own group
```

**Problem:** Looks OK, but what if scope["user"] is spoofed?

**Fix Required:** Force **explicit subscription**:
```python
async def subscribe_activities(self, event):
    # Client requests: { type: "subscribe", user_id: 123 }
    # MUST match authenticated user
    requested = event.get("user_id")
    if requested != self.user.id:
        await self.send({"error": "unauthorized"})
        return
```

---

## 6. Observability Gaps

### Issue 6.1: No Distributed Tracing

**MEDIUM** - No way to trace activity across scheduler → queue → worker → WebSocket.

**Fix Required:** Add **correlation IDs**:
```python
# Generate at scheduler
correlation_id = uuid.uuid4().hex

# Pass through entire chain
dispatch_activity.delay(activity_id, correlation_id=correlation_id)

# Include in all logs and events
logger.info("activity_event", extra={"correlation_id": correlation_id})
```

### Issue 6.2: No Scheduler Metrics

**MEDIUM** - Only worker metrics defined.

**Fix Required:** Add scheduler metrics:
```python
activities_poll_total = Counter("activities_poll_total", "Polls", ["result"])
activities_due_found = Counter("activities_due_found", "Due activities", ["found"])
```

### Issue 6.3: No WebSocket Metrics

**MEDIUM** - No visibility into WebSocket health.

**Fix Required:** Add:
```python
websocket_connections = Gauge("websocket_active_connections", "Active WS connections")
websocket_messages_sent = Counter("websocket_messages_sent_total", "Messages sent")
websocket_messages_acked = Counter("websocket_messages_acked_total", "Acks received")
```

### Issue 6.4: No Lock Contention Metrics

**MEDIUM** - Can't see if locks are causing delays.

**Fix Required:** Add:
```python
activity_lock_acquired = Counter("activity_lock_acquired_total", "Locks acquired", ["result"])
activity_lock_wait_seconds = Histogram("activity_lock_wait_seconds", "Lock wait time")
```

---

## 7. Scheduler Race Conditions

### Issue 7.1: Dispatch Race - Same Activity to Multiple Workers

**CRITICAL** - If scheduler dispatches before acquiring lock:

```
Worker A: SELECT ... WHERE id=1 AND status=PENDING → gets row
Worker B: SELECT ... WHERE id=1 AND status=PENDING → also gets row
Worker A: UPDATE status=RUNNING → succeeds
Worker B: UPDATE status=RUNNING → also succeeds (same value!)
```

**Fix Required:** Use **atomic dispatch**:
```python
# In scheduler - single atomic operation
dispatched = Activity.objects.filter(
    id=activity_id,
    status=Activity.Status.PENDING
).update(
    status=Activity.Status.RUNNING,
    execution_started_at=timezone.now()
)

if not dispatched:
    return {"status": " contention"}  # Already taken
```

### Issue 7.2: Recurrence Race - Double Reschedule

**MEDIUM** - If handler completes while scheduler is also checking.

**Fix Required:** Use **atomic recurrence**:
```python
# Create next occurrence AFTER marking current as done
# Use transaction with select_for_update
with transaction.atomic():
    activity = Activity.objects.select_for_update().get(id=activity_id)
    if activity.status != Activity.Status.RUNNING:
        return
    
    activity.status = Activity.Status.DONE
    activity.last_executed_at = timezone.now()
    activity.save()
    
    if activity.recurrence_type == Activity.RecurrenceType.INTERVAL:
        # Create in SAME transaction
        Activity.objects.create(
            owner=activity.owner,
            farm=activity.farm,
            type=activity.type,
            scheduled_at=activity.next_due_at,
            next_due_at=activity.next_due_at + timedelta(days=activity.interval_days),
            recurrence_type=activity.recurrence_type,
            interval_days=activity.interval_days,
            metadata=activity.metadata,
        )
```

---

## 8. Additional Production Gaps

### Issue 8.1: No Rate Limiting

**HIGH** - API endpoint has no rate limiting.

**Fix Required:** Add throttle:
```python
from rest_framework.throttling import UserRateThrottle

class ActivityThrottle(UserRateThrottle):
    rate = "100/hour"
```

### Issue 8.2: No Input Validation on Metadata

**MEDIUM** - Arbitrary JSON allowed in metadata.

**Fix Required:** Add schema validation:
```python
METADATA_SCHEMAS = {
    "vaccination": {
        "required": ["cattle_id"],
        "properties": {"cattle_id": {"type": "integer"}, "vaccine": {"type": "string"}}
    },
    # ...
}

def validate_metadata(activity_type, metadata):
    schema = METADATA_SCHEMAS.get(activity_type)
    if not schema:
        return True  # No schema = allow all
    
    # Validate against JSON schema
    return jsonschema.validate(metadata, schema)
```

### Issue 8.3: No Health Check Endpoint

**MEDIUM** - Can't monitor system health.

**Fix Required:** Add:
```python
# /health/activities/
def health_check(request):
    # Check Redis connection
    # Check DB connection
    # Check scheduler last run
    return Response({"status": "healthy"})
```

### Issue 8.4: Handler Error Classification Missing

**MEDIUM** - All exceptions treated the same.

**Fix Required:** Add handler exception hierarchy:
```python
class HandlerError(Exception):
    pass

class TemporaryError(HandlerError):
    """Retryable"""
    pass

class PermanentError(HandlerError):
    """Do not retry"""
    pass
```

---

## Summary: Required Changes

| Priority | Issue | Section | Fix |
|----------|-------|---------|-----|
| CRITICAL | Split-brain locking | 1.1 | Single-phase acquire/release |
| CRITICAL | No execution timeout | 2.1 | Add time_limit |
| CRITICAL | Fire-and-forget WebSocket | 3.1 | Store-and-forward |
| CRITICAL | No JWT WebSocket auth | 5.1 | JWT middleware |
| CRITICAL | Dispatch race | 7.1 | Atomic dispatch |
| HIGH | Lost activity recovery | 2.2 | Schedule recovery task |
| HIGH | DB growth | 4.1 | Auto-archive |
| HIGH | No rate limiting | 8.1 | Add throttle |
| MEDIUM | Atomic recurrence | 7.2 | Transaction + lock |
| MEDIUM | Check-then-act race | 1.2 | Atomic update |
| MEDIUM | Multiple schedulers | 1.3 | Leader election |
| MEDIUM | Tracing correlation | 6.1 | Add correlation_id |
| MEDIUM | Handler exceptions | 8.4 | Exception hierarchy |

---

## Verdict: REQUIRES HARDENING BEFORE IMPLEMENTATION

**The TDD must be updated with these fixes before implementation begins.**

---

## Document Control

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | May 3, 2026 | opencode | Initial hardening review |

---

**END OF HARDENING REVIEW**