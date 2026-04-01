#!/bin/sh
set -eu

MASTER_NAME="${REDIS_MASTER_NAME:-redis-master}"
SENTINEL_PORT="${SENTINEL_PORT:-26379}"
TRIES=${SENTINEL_START_TRIES:-60}

while ! getent hosts "$MASTER_NAME" >/dev/null 2>&1; do
  echo "waiting for $MASTER_NAME to become resolvable"
  TRIES=$((TRIES - 1))
  if [ "$TRIES" -le 0 ]; then
    echo "failed to resolve $MASTER_NAME" >&2
    exit 1
  fi
  sleep 1
done

MASTER_IP=$(getent hosts "$MASTER_NAME" | awk '{print $1}')
echo "resolved $MASTER_NAME -> $MASTER_IP"

echo "writing sentinel config"
 cat <<CONF >/tmp/sentinel.conf
 port $SENTINEL_PORT
 sentinel monitor mymaster $MASTER_IP 6379 2
sentinel down-after-milliseconds mymaster 5000
sentinel failover-timeout mymaster 10000
sentinel parallel-syncs mymaster 1
sentinel deny-scripts-reconfig yes
CONF

exec redis-server /tmp/sentinel.conf --sentinel
