# Performance Tuning Deployment Guide

**Date:** April 4, 2026  
**Purpose:** Fix server degradation after ~10 hours of operation

---

## ⚠️ Security Warning

**IMPORTANT:** You shared your sudo password in plain text. Please **change it immediately** after completing this deployment:

```bash
passwd
```

---

## Quick Summary

| Component | Issue | Fix |
|-----------|-------|-----|
| **Nextcloud PHP-FPM** | Unlimited workers, memory leaks | Cap workers + auto-restart |
| **MySQL** | Unbounded buffer pool | Set `innodb_buffer_pool_size = 1G` |
| **Redis** | No memory limit, grows indefinitely | Set `maxmemory 512mb` |
| **Celery** | Workers leak memory over time | Auto-restart after 100 tasks |
| **Prometheus** | No retention, disk fills up | 7-day retention, 2GB cap |
| **Loki** | Logs grow indefinitely | 7-day retention |
| **Monitoring** | Missing memory/resource alerts | Added comprehensive alerts |

---

## Phase 1: Docker Monitoring Stack (5 minutes)

### 1.1 Restart Monitoring Stack

The following files have been updated in your repository:
- ✅ `docker-compose.monitoring.yml` (Redis limits, Prometheus retention)
- ✅ `monitoring/loki/loki.yml` (7-day retention)
- ✅ `monitoring/prometheus/alerts.yml` (Memory/resource alerts)

**Deploy changes:**

```bash
cd /home/rahim/projects/weather-apis

# Stop monitoring stack
docker-compose -f docker-compose.monitoring.yml down

# Start with new config
docker-compose -f docker-compose.monitoring.yml up -d

# Verify containers are running
docker-compose -f docker-compose.monitoring.yml ps
```

**Expected output:**
```
NAME                           STATUS
weather-apis-prometheus-1      Up
weather-apis-grafana-1         Up
weather-apis-loki-1            Up
weather-apis-promtail-1        Up
weather-apis-redis-1           Up
weather-apis-node-exporter-1   Up
weather-apis-blackbox-1        Up
weather-apis-redis_exporter-1  Up
```

### 1.2 Verify Prometheus Retention

```bash
# Check Prometheus retention settings
docker exec weather-apis-prometheus-1 prometheus --version

# Verify flags
docker exec weather-apis-prometheus-1 ps aux | grep retention
```

**Expected:** `--storage.tsdb.retention.time=7d --storage.tsdb.retention.size=2GB`

### 1.3 Verify Redis Memory Limits

```bash
# Check Redis memory config
docker exec weather-apis-redis-1 redis-cli CONFIG GET maxmemory
docker exec weather-apis-redis-1 redis-cli CONFIG GET maxmemory-policy
```

**Expected:**
```
1) "maxmemory"
2) "536870912"  ; 512MB in bytes
1) "maxmemory-policy"
2) "allkeys-lru"
```

---

## Phase 2: Nextcloud PHP-FPM Tuning (10 minutes)

### 2.1 Check Current PHP Version

```bash
php -v
# Note the version (e.g., 8.1, 8.2)
```

### 2.2 Deploy PHP-FPM Config

```bash
# Copy tuning config (adjust PHP version as needed)
sudo cp /home/rahim/projects/weather-apis/monitoring/tuning-configs/nextcloud-php-fpm.conf \
  /etc/php/8.1/fpm/pool.d/nextcloud.conf

# Replace placeholder PHP version in config
sudo sed -i 's/php8.1-fpm/php8.1-fpm/g' /etc/php/8.1/fpm/pool.d/nextcloud.conf

# Verify config syntax
sudo php-fpm8.1 -t
```

### 2.3 Restart PHP-FPM

```bash
sudo systemctl restart php8.1-fpm
sudo systemctl status php8.1-fpm
```

### 2.4 Verify PHP-FPM Workers

```bash
# Check worker count
ps aux | grep php-fpm | grep -c "pool nextcloud"

# Check memory per worker
ps aux | grep php-fpm | awk '{print $6/1024 " MB - " $11}'
```

**Expected:**
- Max 15 workers
- Each worker < 512MB

---

## Phase 3: MySQL/MariaDB Tuning (10 minutes)

### 3.1 Deploy MySQL Config

```bash
# Copy tuning config
sudo cp /home/rahim/projects/weather-apis/monitoring/tuning-configs/mysql-tuning.cnf \
  /etc/mysql/mariadb.conf.d/99-weather-apis.cnf

# Verify syntax
mysqld --validate-config
```

### 3.2 Restart MySQL

```bash
sudo systemctl restart mysql
# Or for MariaDB:
sudo systemctl restart mariadb

# Check status
sudo systemctl status mysql
```

### 3.3 Verify Settings

```bash
# Check buffer pool size
mysql -e "SHOW VARIABLES LIKE 'innodb_buffer_pool_size';"

# Check max connections
mysql -e "SHOW VARIABLES LIKE 'max_connections';"

# Check query cache (MariaDB only)
mysql -e "SHOW VARIABLES LIKE 'query_cache_%';"
```

**Expected:**
```
innodb_buffer_pool_size = 1073741824  ; 1GB
max_connections = 100
```

### 3.4 Monitor Slow Queries

```bash
# Watch slow query log
sudo tail -f /var/log/mysql/slow.log

# Or query MySQL for slow queries
mysql -e "SELECT * FROM mysql.slow_log ORDER BY start_time DESC LIMIT 10;"
```

---

## Phase 4: Redis Tuning (5 minutes)

### 4.1 Check if Redis is System or Docker

```bash
# Check if Redis is running as system service
sudo systemctl status redis

# OR check if it's in Docker
docker ps | grep redis
```

### Option A: System Redis

```bash
# Backup current config
sudo cp /etc/redis/redis.conf /etc/redis/redis.conf.backup.$(date +%Y%m%d)

# Append tuning config
cat /home/rahim/projects/weather-apis/monitoring/tuning-configs/redis-tuning.conf | \
  sudo tee -a /etc/redis/redis.conf

# Restart Redis
sudo systemctl restart redis-server
sudo systemctl status redis-server
```

### Option B: Docker Redis

If Redis is part of the monitoring stack, it's already configured in Phase 1.

If you have a separate Docker Redis for Celery/Django:

```bash
# Update docker-compose.yml to use tuning config
# Add volume mount:
# volumes:
#   - ./monitoring/tuning-configs/redis-tuning.conf:/etc/redis/redis.conf:ro

# Restart Redis container
docker-compose restart redis
```

### 4.2 Verify Redis Limits

```bash
redis-cli INFO memory | grep -E "used_memory_human|maxmemory_human|maxmemory_policy"
```

**Expected:**
```
used_memory_human: 45.23M
maxmemory_human: 512.00M
maxmemory_policy: allkeys-lru
```

---

## Phase 5: Celery Worker Tuning (5 minutes)

### 5.1 Deploy Celery Config

The Celery memory limits have been added to `config/celery.py`:
- ✅ `worker_max_tasks_per_child=100`
- ✅ `worker_max_memory_per_child=512000` (512MB)
- ✅ `task_time_limit=300` (5 min hard limit)
- ✅ `task_soft_time_limit=240` (4 min soft limit)

### 5.2 Restart Celery Workers

```bash
# If using systemd
sudo systemctl restart celery-worker
sudo systemctl restart celery-beat

# If running manually
# Kill existing workers
pkill -f celery

# Restart with new config
cd /home/rahim/projects/weather-apis
celery -A config.celery worker -l info -Q celery,ndvi --concurrency=4 &
celery -A config.celery beat -l info &
```

### 5.3 Verify Celery Settings

```bash
# Check Celery worker config
celery -A config.celery inspect conf

# Or check worker logs
sudo journalctl -u celery-worker -f
```

**Expected:** Workers should restart automatically after 100 tasks or if memory exceeds 512MB.

---

## Phase 6: Nextcloud Log Rotation (5 minutes)

### 6.1 Rotate Current Log

```bash
# Check current log size
ls -lh /var/www/html/nextcloud/data/nextcloud.log

# Rotate log
sudo -u www-data mv /var/www/html/nextcloud/data/nextcloud.log \
  /var/www/html/nextcloud/data/nextcloud.log.old

# Create new empty log
sudo -u www-data touch /var/www/html/nextcloud/data/nextcloud.log
sudo -u www-data chmod 640 /var/www/html/nextcloud/data/nextcloud.log

# Compress old log
sudo gzip /var/www/html/nextcloud/data/nextcloud.log.old
```

### 6.2 Configure Log Rotation in Nextcloud

Edit `/var/www/html/nextcloud/config/config.php`:

```bash
sudo nano /var/www/html/nextcloud/config/config.php
```

Add/update these settings:

```php
<?php
$CONFIG = array(
  // ... existing config ...
  
  // Log rotation
  'log_type' => 'file',
  'logfile' => '/var/www/html/nextcloud/data/nextcloud.log',
  'loglevel' => 2,  // Warning only (0=Debug, 1=Info, 2=Warning, 3=Error)
  'log_rotate_size' => 10 * 1024 * 1024,  // 10MB rotation
  
  // Reduce background job load
  'filesystem_check_changes' => 0,  // Disable file scanning (use cron instead)
  
  // Preview limits
  'enable_previews' => true,
  'preview_max_x' => 1024,
  'preview_max_y' => 1024,
  'preview_max_memory' => 256,  // MB
);
```

### 6.3 Set Up Systemd Log Rotation

```bash
sudo nano /etc/logrotate.d/nextcloud
```

Add:

```
/var/www/html/nextcloud/data/nextcloud.log {
    weekly
    missingok
    rotate 4
    compress
    delaycompress
    notifempty
    create 0640 www-data www-data
    sharedscripts
    postrotate
        systemctl reload php8.1-fpm > /dev/null 2>&1 || true
    endscript
}
```

Test logrotate:

```bash
sudo logrotate -d /etc/logrotate.d/nextcloud
```

---

## Phase 7: Verify All Changes (10 minutes)

### 7.1 Run Diagnostic Script

```bash
#!/bin/bash
# save as /tmp/check-server-health.sh

echo "=== Memory Usage ==="
free -h
ps aux --sort=-%mem | head -n 10

echo -e "\n=== Docker Container Memory ==="
docker stats --no-stream --format "table {{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}"

echo -e "\n=== PHP-FPM Workers ==="
ps aux | grep php-fpm | grep -c "pool nextcloud"
ps aux | grep php-fpm | awk '{sum += $6} END {print "Total PHP-FPM memory: " sum/1024 " MB"}'

echo -e "\n=== MySQL Connections ==="
mysql -e "SHOW STATUS LIKE 'Threads_connected';"
mysql -e "SHOW VARIABLES LIKE 'max_connections';"

echo -e "\n=== Redis Memory ==="
redis-cli INFO memory | grep -E "used_memory_human|maxmemory_human"

echo -e "\n=== Disk Usage ==="
df -h
du -sh /var/www/html/nextcloud/data/nextcloud.log 2>/dev/null || echo "Log not found"
du -sh /var/lib/docker/volumes/weather-apis_prometheus_data/_data 2>/dev/null || echo "Prometheus data not found"

echo -e "\n=== OOM Killer Logs ==="
dmesg | grep -i oom | tail -n 5 || echo "No OOM events"
journalctl -k | grep -i "killed process" | tail -n 5 || echo "No killed processes"

echo -e "\n=== Celery Workers ==="
ps aux | grep celery | grep -v grep
```

Make executable and run:

```bash
chmod +x /tmp/check-server-health.sh
/tmp/check-server-health.sh
```

### 7.2 Monitor for 24 Hours

```bash
# Watch memory usage over time
watch -n 60 'free -m | head -n 2'

# Watch Docker stats
watch -n 30 'docker stats --no-stream'

# Watch Prometheus alerts
curl -s http://localhost:9090/api/v1/alerts | jq '.data.alerts[] | {name: .labels.alertname, state: .state}'
```

---

## Phase 8: Monitor Alerts in Grafana

### 8.1 Access Grafana

```bash
# Open browser to:
https://desktop-scbdcpm.tail4a3842.ts.net/grafana/

# Login:
Username: admin
Password: admin
```

### 8.2 Check New Alerts

Navigate to **Alerting → Alert Rules** and verify:
- ✅ MemoryUsageHigh
- ✅ MemoryUsageCritical
- ✅ DiskSpaceLowRoot
- ✅ RedisMemoryNearLimit
- ✅ RedisEvictionStarted
- ✅ NextcloudLogFileLarge

---

## Expected Results After Tuning

| Metric | Before | After |
|--------|--------|-------|
| **Memory stability** | Degrades after 10hrs | Stable for weeks |
| **PHP-FPM workers** | Unlimited, leak memory | Max 15, restart after 500 requests |
| **MySQL memory** | Unbounded | Capped at ~1.5GB total |
| **Redis memory** | Grows indefinitely | Capped at 512MB |
| **Prometheus disk** | Fills up | Auto-deletes after 7 days / 2GB |
| **Loki disk** | Fills up | Auto-deletes after 7 days |
| **Celery workers** | Memory leaks | Auto-restart after 100 tasks |
| **Nextcloud log** | Grows to GBs | Rotated at 10MB |

---

## Rollback Plan

If any tuning causes issues, rollback is simple:

### PHP-FPM Rollback
```bash
sudo rm /etc/php/8.1/fpm/pool.d/nextcloud.conf
sudo systemctl restart php8.1-fpm
```

### MySQL Rollback
```bash
sudo rm /etc/mysql/mariadb.conf.d/99-weather-apis.cnf
sudo systemctl restart mysql
```

### Redis Rollback
```bash
sudo cp /etc/redis/redis.conf.backup.* /etc/redis/redis.conf
sudo systemctl restart redis-server
```

### Docker Monitoring Rollback
```bash
cd /home/rahim/projects/weather-apis
git checkout HEAD -- docker-compose.monitoring.yml monitoring/
docker-compose -f docker-compose.monitoring.yml down
docker-compose -f docker-compose.monitoring.yml up -d
```

### Celery Rollback
```bash
cd /home/rahim/projects/weather-apis
git checkout HEAD -- config/celery.py
sudo systemctl restart celery-worker
```

---

## Troubleshooting

### Server Still Crashing After 10hrs

1. Check OOM killer:
   ```bash
   dmesg | grep -i oom
   ```

2. Identify top memory consumer:
   ```bash
   ps aux --sort=-%mem | head -n 10
   ```

3. Check Docker logs:
   ```bash
   journalctl -u docker --since "1 hour ago" | grep -i oom
   ```

### PHP-FPM Workers Still Leaking

1. Verify config loaded:
   ```bash
   sudo php-fpm8.1 -i | grep max_requests
   ```

2. Check pool status:
   ```bash
   sudo systemctl status php8.1-fpm
   ```

3. Force restart all workers:
   ```bash
   sudo systemctl restart php8.1-fpm
   ```

### MySQL Connections Exhausted

1. Check current connections:
   ```bash
   mysql -e "SHOW STATUS LIKE 'Threads_connected';"
   ```

2. Identify long-running queries:
   ```bash
   mysql -e "SHOW FULL PROCESSLIST;"
   ```

3. Kill stuck queries:
   ```bash
   mysql -e "KILL <process_id>;"
   ```

### Redis Evicting Too Many Keys

1. Check eviction rate:
   ```bash
   redis-cli INFO stats | grep evicted_keys
   ```

2. Increase memory limit:
   ```bash
   redis-cli CONFIG SET maxmemory 1gb
   ```

3. Identify largest keys:
   ```bash
   redis-cli --bigkeys
   ```

---

## Post-Deployment Checklist

- [ ] PHP-FPM workers capped at 15
- [ ] MySQL buffer pool set to 1GB
- [ ] Redis maxmemory set to 512MB
- [ ] Celery workers restart after 100 tasks
- [ ] Prometheus retention set to 7 days / 2GB
- [ ] Loki retention set to 7 days
- [ ] Nextcloud log rotation configured
- [ ] Memory alerts visible in Grafana
- [ ] No OOM killer events in dmesg
- [ ] Server stable after 24 hours

---

## Next Steps After Stabilization

1. **Monitor for 1 week** to ensure stability
2. **Fine-tune values** based on actual usage patterns
3. **Consider adding swap** if memory is consistently tight:
   ```bash
   sudo fallocate -l 2G /swapfile
   sudo chmod 600 /swapfile
   sudo mkswap /swapfile
   sudo swapon /swapfile
   echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
   ```
4. **Set up automated backups** of MySQL and Nextcloud data
5. **Review slow query log** weekly and optimize queries
6. **Consider horizontal scaling** if single server can't handle load

---

## Support & Monitoring

If issues persist after applying all tuning:

1. **Check Grafana dashboards** for resource usage trends
2. **Review Prometheus alerts** for specific bottleneck
3. **Analyze Nextcloud admin overview** at `/index.php/settings/admin`
4. **Review MySQL slow query log** for expensive queries
5. **Check Celery worker logs** for stuck tasks
6. **Monitor Redis memory usage** for unexpected growth

---

**Document Version:** 1.0  
**Last Updated:** April 4, 2026  
**Maintained By:** Weather APIs Team
