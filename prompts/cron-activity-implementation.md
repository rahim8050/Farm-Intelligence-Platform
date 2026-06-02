# Cron Activity Scheduling — Implementation Report

## Task

Implement cron-based activity scheduling in the `activities` app. The `RecurrenceType.CRON`
option existed in the model but was explicitly labeled `"Cron (future)"` with no
implementation — no cron expression field, no validation, no parser, and no scheduler wiring.

## What was done

### 1. Model (`activities/models.py`)

- Added `cron_expression = models.CharField(max_length=100, null=True, blank=True)`
- Changed `RecurrenceType.CRON` label from `"Cron (future)"` → `"Cron"`
- Implemented `_parse_cron_field()` — static method that parses a single cron field
  (supports `*`, `N`, `N-M`, `N,M,O`, `*/N`, `N-M/N`) into a set of allowed values
- Implemented `_compute_cron_next()` — static method that takes a 5-field cron expression
  and a reference datetime, iterates forward minute-by-minute (up to ~1 year) to find
  the next matching datetime. Standard cron format: `minute hour day_of_month month day_of_week`
- Updated `save()` to compute `next_due_at` from `cron_expression` when
  `recurrence_type='cron'` and `next_due_at is None`

### 2. Migration

- `activities/migrations/0003_add_cron_expression.py`

### 3. Serializer (`activities/serializers.py`)

- Added `cron_expression` to both `ActivitySerializer` and `ActivityCreateSerializer`
- Validation: `cron_expression` is required when `recurrence_type='cron'`
- Validation: invalid cron expressions are rejected via `Activity._compute_cron_next()`

### 4. Services (`activities/services.py`)

- Added `reschedule_recurring(activity)` — after successful execution, if the activity
  has `cron` or `interval` recurrence, computes the next `next_due_at` and transitions
  status back to `PENDING` for scheduler re-pickup

### 5. Tasks (`activities/tasks.py`)

- Modified `execute_activity` to call `reschedule_recurring()` after `transition_to_success()`

### 6. Celery Beat (`config/settings.py`)

- Added `activities-scheduler-poll` → `activities.scheduler.poll` (every 60s)
- Added `activities-recover-stale` → `activities.recover_stale` (every 300s)

These tasks were defined but never registered in `CELERY_BEAT_SCHEDULE`.

### 7. Tests

9 new tests in `activities/tests/test_activities.py`:

| Test | What it covers |
|------|---------------|
| `test_validate_cron_requires_expression` | Rejects cron without expression |
| `test_validate_cron_invalid_expression` | Rejects bad cron string |
| `test_valid_cron_passes` | Accepts valid cron |
| `test_cron_save_computes_next_due` | Model save computes next_due_at |
| `test_cron_save_rejects_invalid_expression` | Model save raises on bad expression |
| `test_reschedule_cron_recurring` | Resets to PENDING with next due |
| `test_reschedule_none_does_nothing` | Skips non-recurring |
| `test_create_activity_with_cron` | API creates with cron_expression |
| `test_create_activity_cron_missing_expression` | API rejects missing expression |
| `test_create_activity_cron_invalid_expression` | API rejects bad expression |

### 8. Documentation

- `activities/README.md`: cron no longer listed as "future", API docs include
  `cron_expression`, new curl example for cron activity creation

### 9. Reports

- `reports/2026-06-01.md` — yesterday's report
- `reports/2026-06-02.md` — today's report

## Architecture flow

```
User creates activity with recurrence_type=cron, cron_expression="30 9 * * 1-5"
  → Activity.save() computes next_due_at as first cron match >= scheduled_at
  → Activity sits in PENDING until next_due_at passes
  → activities.scheduler.poll picks it up (every 60s via Celery Beat)
  → claim_activity() atomically sets status=DISPATCHED
  → execute_activity.delay() runs the handler
  → Handler executes, transition_to_success()
  → reschedule_recurring() computes next cron match, sets status=PENDING
  → Activity is ready for next scheduler cycle
```

## Cron parser details

The parser lives in `Activity._compute_cron_next()` and supports standard 5-field cron:
- Field 1: minute (0-59)
- Field 2: hour (0-23)
- Field 3: day of month (1-31)
- Field 4: month (1-12)
- Field 5: day of week (0=Sunday, 6=Saturday)

Each field supports `*`, `N`, `N-M`, `N,M,O`, `*/N`, and `N-M/N` syntax.

The algorithm iterates forward from the reference datetime (ceiled to next minute) and
checks each field from coarse (month) to fine (minute). On mismatch, it advances the
coarse field and resets finer fields to their minimum. Worst-case iteration is ~525,600
minutes (1 year) — the loop raises `ValueError` if no match is found within that window.

The parser was written in-house (instead of using `croniter` or Celery's `crontab`)
because:
- No network access was available to install `croniter`
- Celery's `crontab.remaining_estimate()` has edge-case bugs when the reference time
  is in the past relative to the schedule

## Files changed

| File | Change |
|------|--------|
| `activities/models.py` | Added cron_expression field, parser, updated save() |
| `activities/serializers.py` | Added cron_expression to fields + validation |
| `activities/services.py` | Added reschedule_recurring() |
| `activities/tasks.py` | execute_activity calls reschedule after success |
| `config/settings.py` | Added poll + recover_stale to CELERY_BEAT_SCHEDULE |
| `activities/views.py` | Added cron_expression to OpenAPI list serializer |
| `activities/migrations/0003_add_cron_expression.py` | New migration |
| `activities/tests/test_activities.py` | 10 new tests |
| `activities/README.md` | Updated docs |
| `reports/2026-06-01.md` | New report |
| `reports/2026-06-02.md` | New report |

## Verification

- `ruff check .` — all checks passed
- `ruff format .` — clean
- `mypy activities/` — no issues
- `pytest activities/tests/test_activities.py -k "cron"` — 10/10 passed
