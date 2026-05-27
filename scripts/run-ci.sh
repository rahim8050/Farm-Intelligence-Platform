#!/usr/bin/env bash
set -euo pipefail

CI_IMAGE="${CI_IMAGE:-weather-apis-ci}"
MYSQL_CONTAINER="${MYSQL_CONTAINER:-ci-mysql}"
CI_CONTAINER="${CI_CONTAINER:-ci-runner}"
NETWORK="${NETWORK:-ci-net}"
MYSQL_ROOT_PASS="${MYSQL_ROOT_PASS:-root_pass}"
MYSQL_DB="${MYSQL_DB:-test_db}"
MYSQL_USER="${MYSQL_USER:-test_user}"
MYSQL_PASS="${MYSQL_PASS:-test_pass}"

cleanup() {
  echo "=== Cleaning up..."
  docker rm -f "$CI_CONTAINER" 2>/dev/null || true
  docker rm -f "$MYSQL_CONTAINER" 2>/dev/null || true
  docker network rm "$NETWORK" 2>/dev/null || true
}
trap cleanup EXIT

echo "=== Building CI image..."
docker build -t "$CI_IMAGE" -f Dockerfile.ci .

echo "=== Creating network..."
docker network create "$NETWORK" 2>/dev/null || echo "Network already exists"

echo "=== Starting MySQL..."
docker rm -f "$MYSQL_CONTAINER" 2>/dev/null || true
docker run -d \
  --name "$MYSQL_CONTAINER" \
  --network "$NETWORK" \
  -e MYSQL_DATABASE="$MYSQL_DB" \
  -e MYSQL_USER="$MYSQL_USER" \
  -e MYSQL_PASSWORD="$MYSQL_PASS" \
  -e MYSQL_ROOT_PASSWORD="$MYSQL_ROOT_PASS" \
  mysql:8.0

echo "=== Waiting for MySQL..."
for i in $(seq 1 30); do
  if docker exec "$MYSQL_CONTAINER" mysqladmin ping -h 127.0.0.1 -p"$MYSQL_ROOT_PASS" >/dev/null 2>&1; then
    echo "MySQL is ready."
    break
  fi
  echo "Waiting... ($i/30)"
  sleep 2
done

echo "=== Running checks..."
docker run --rm \
  --name "$CI_CONTAINER" \
  --network "$NETWORK" \
  -v "$(pwd):/app" \
  -e DJANGO_ENV=ci \
  -e DJANGO_DEBUG=0 \
  -e DJANGO_SECRET_KEY=ci-secret-key-not-for-prod \
  -e DJANGO_API_KEY_PEPPER=ci-pepper-not-a-secret \
  -e MYSQL_HOST="$MYSQL_CONTAINER" \
  -e MYSQL_PORT=3306 \
  -e MYSQL_DATABASE="$MYSQL_DB" \
  -e MYSQL_USER="$MYSQL_USER" \
  -e MYSQL_PASSWORD="$MYSQL_PASS" \
  -e DATABASE_URL="mysql://$MYSQL_USER:$MYSQL_PASS@$MYSQL_CONTAINER:3306/$MYSQL_DB" \
  -e COVERAGE_FAIL_UNDER="${COVERAGE_FAIL_UNDER:-96}" \
  "$CI_IMAGE" \
  bash -c '
set -euo pipefail

echo "--- Ruff lint ---"
python -m ruff check .

echo "--- Ruff format check ---"
python -m ruff format --check .

echo "--- MyPy ---"
python -m mypy --config-file=pyproject.toml .

echo "--- Bandit ---"
python -m bandit -c pyproject.toml -r .

echo "--- Django system checks ---"
python manage.py check
python manage.py check --deploy

echo "--- Migration consistency ---"
python manage.py makemigrations --check --dry-run

echo "--- Migrate ---"
python manage.py migrate --no-input

echo "--- Pytest with coverage (gate: ${COVERAGE_FAIL_UNDER}%) ---"
python -m pytest -q \
  -x \
  --durations=5 \
  --cov=. \
  --cov-report=term-missing \
  --cov-report=xml \
  --cov-fail-under="${COVERAGE_FAIL_UNDER}"

echo ""
echo "=== All CI checks passed ==="
'
