# Testing

This repo uses pytest + pytest-django.

## Run the test suite

```bash
uv run pytest
```

## Run tests with coverage

```bash
uv run pytest --cov=. --cov-report=term-missing --cov-report=xml
```

## Enforce the coverage gate locally

```bash
COVERAGE_FAIL_UNDER=98 uv run pytest --cov=. --cov-report=term-missing \
  --cov-report=xml --cov-fail-under="${COVERAGE_FAIL_UNDER}"
```
