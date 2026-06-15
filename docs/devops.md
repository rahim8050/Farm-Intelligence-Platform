# Prometheus + Django Metrics on Windows 10/WSL2

This document is a production-grade onboarding and troubleshooting guide for anyone standing up Prometheus scraping of Django `/metrics` in a Windows 10 + WSL2 development environment. It is written for DevOps engineers, backend developers, and automation scripts so you can follow the steps verbatim, automate them in onboarding tooling, and troubleshoot edge cases without guesswork.

---

## 1. Scope & audience

- **Stack**
  - **Backend:** Django (Python) exposing `/metrics` through `django-prometheus`.
  - **Monitoring:** Prometheus (`prom/prometheus:latest`) running inside Docker Desktop.
  - **OS:** Windows 10 with WSL2 integration.
  - **Networking:** `host.docker.internal`, Windows Defender Firewall, and `netsh interface portproxy`.

- **Audience**
  - DevOps engineers provisioning monitoring environments.
  - Backend developers verifying metrics integrations locally.
  - Onboarding automation scripts or agents that configure/check infrastructure.

---

## 2. Architecture & network picture

```text
[Developer machine (HTTP client)]
          |
      Windows host
    (Firewall + NAT)
          |
  Docker Desktop bridge
          |
[Prometheus container]
        ↔ host.docker.internal
          |
   WSL2 virtual switch
          |
[Django on 0.0.0.0:8000 → /metrics]
```

- Containers reach the host via `host.docker.internal`.  
- Traffic flows through Windows Defender Firewall and Docker’s NAT before it hits WSL2.  
- WSL IP addresses change on reboot—do not hardcode them except in a documented fallback (`netsh portproxy`).

---

## 3. Django configuration (mandatory)

1. **Run Django bound to all interfaces (inside WSL)**

   ```bash
   python manage.py runserver 0.0.0.0:8000
   ```

2. **`ALLOWED_HOSTS`** (in `config/settings.py` or `.env`). Must include:

   ```python
   ALLOWED_HOSTS = ["localhost", "127.0.0.1", "host.docker.internal"]
   ```

3. **Environment hints (optional but recommended)**:

   ```text
   DJANGO_BIND=0.0.0.0
   DJANGO_METRICS_PATH=/metrics
   ```

4. **Verify the metrics endpoint** is registered via `django-prometheus` (middleware + URL config). `/metrics` should respond without authentication.

---

## 4. Prometheus configuration & Docker setup

### 4.1 `prometheus.yml`

```yaml
global:
  scrape_interval: 15s

scrape_configs:
  - job_name: django
    metrics_path: /metrics
    static_configs:
      - targets: ["host.docker.internal:8000"]
```

### 4.2 Docker Compose example (preferred)

```yaml
services:
  prometheus:
    image: prom/prometheus:latest
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml:ro
    extra_hosts:
      - "host.docker.internal:host-gateway"
    ports:
      - "9090:9090"
    depends_on:
      - django
```

`extra_hosts` ensures `host.docker.internal` resolves via Docker’s `host-gateway`, keeping the configuration stable across Docker Desktop updates.

---

## 5. Step-by-step setup & verification

### 5.1 Setup sequence

1. Start Django inside WSL:

   ```bash
   python manage.py runserver 0.0.0.0:8000
   ```

2. Launch Prometheus:

   ```bash
   docker compose up -d prometheus
   ```

3. Confirm local metrics:

   ```bash
   curl http://localhost:8000/metrics | head
   ```

### 5.2 Connectivity checks

- **Inside Prometheus container**:

  ```bash
  docker exec -it farm-intelligence-platform-prometheus-1 sh -c \
    "wget -qO- http://host.docker.internal:8000/metrics | head -n 20"
  ```
- **Simulated request**:
  ```bash
  curl -H "Host: host.docker.internal" \
    http://host.docker.internal:8000/metrics
  ```
- **Prometheus UI**: Visit `http://localhost:9090/targets` and confirm the Django job state is `UP`.

---

## 6. Windows Defender Firewall automation

Run this in an Administrator PowerShell to allow Docker traffic to port 8000:

```powershell
netsh advfirewall firewall add rule `
  name="Allow Django Metrics (8000)" `
  dir=in action=allow protocol=TCP localport=8000 `
  remoteip=10.0.0.0/8,172.16.0.0/12,192.168.0.0/16 `
  profile=Private,Domain
```

### Automate the rule

- Place the command in `scripts/setup-firewall.ps1`.
- Onboarding script snippet:

  ```powershell
  if (-not (Get-NetFirewallRule -DisplayName "Allow Django Metrics (8000)" -ErrorAction SilentlyContinue)) {
    .\scripts\setup-firewall.ps1
  }
  ```
- Run this automatically during developer setup or CI bootstrapping (if allowed).

---

## 7. `netsh interface portproxy` fallback (WSL networking issues)

If the firewall rule still blocks traffic, use `portproxy` to forward host port 8000 to the current WSL IP:

```powershell
netsh interface portproxy add v4tov4 `
  listenport=8000 listenaddress=0.0.0.0 `
  connectport=8000 connectaddress=<current_wsl_ip>
```

- Replace `<current_wsl_ip>` with the WSL IP (`ip addr show eth0` inside WSL).  
- Verify: `netsh interface portproxy show all`.  
- Remove when no longer needed:

  ```powershell
  netsh interface portproxy delete v4tov4 listenport=8000 listenaddress=0.0.0.0
  ```
- Restart Prometheus after applying portproxy so the target resolves the forwarded port.

---

## 8. Troubleshooting checklist

- [ ] Django bound to `0.0.0.0:8000`.  
- [ ] `ALLOWED_HOSTS` includes `host.docker.internal`.  
- [ ] Prometheus targets `host.docker.internal:8000`.  
- [ ] Compose includes `extra_hosts: ["host.docker.internal:host-gateway"]`.  
- [ ] Windows firewall rule allows Docker/WSL subnets.  
- [ ] `wget/curl` inside Prometheus container returns metrics.  
- [ ] Prometheus UI `/targets` shows the job `UP`.  
- [ ] Reserve `netsh portproxy` only if the host gateway remains unreachable.

---

## 9. Reliability & automation guidance

- **Onboarding script flow**:
  1. Start Django from WSL.  
  2. Apply the firewall rule (PowerShell snippet).  
  3. Launch Prometheus container.  
  4. Run `docker exec … wget …` as a smoke test.  
  5. Exit non-zero if the scrape fails (so automation knows to retry).
- **Agent prompt**: “Ensure the firewall rule exists and the Prometheus container can GET `/metrics` via `host.docker.internal` before declaring the environment ready.”
- **CI/Linux runners**:
  - `host.docker.internal` is unavailable. Run Django and Prometheus in the same Docker Compose network.
  - Example:
    ```yaml
    services:
      django:
        build: .
        ports:
          - "8000:8000"
      prometheus:
        depends_on:
          - django
        networks:
          - monitoring
    networks:
      monitoring:
        driver: bridge
    ```
  - Prometheus should scrape `django:8000`.

---

## 10. Prometheus query examples

```promql
rate(django_http_requests_total[5m])
sum by (status_code)(increase(django_http_requests_total[5m]))
```

Use the `/graph` UI to iteratively validate metric availability once the scrape succeeds.

---

## 11. References

- [django-prometheus README](https://github.com/korfuri/django-prometheus)  
- [Prometheus static_configs docs](https://prometheus.io/docs/prometheus/latest/configuration/configuration/#static_config)  
- [Docker Desktop host-gateway documentation](https://docs.docker.com/desktop/networking/#use-cases-and-workarounds)  
- [Windows Defender Firewall command line](https://learn.microsoft.com/windows/security/threat-protection/windows-firewall/create-an-inbound-port-rule)  
- [Netsh interface portproxy](https://learn.microsoft.com/windows-server/administration/windows-commands/netsh-interface-portproxy)
