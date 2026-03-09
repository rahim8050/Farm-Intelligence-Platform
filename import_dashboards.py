#!/usr/bin/env python3
"""Import Grafana dashboards from JSON files."""

import json
import os
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    print("Installing requests...")
    os.system("pip install requests")  # noqa: S605,S607
    import requests

GRAFANA_URL = "http://127.0.0.1:3001"
DASHBOARDS_DIR = Path(__file__).parent / "grafana" / "dashboards"


def import_dashboard(session: requests.Session, dashboard_file: Path) -> bool:
    """Import a single dashboard."""
    print(f"\nImporting {dashboard_file.name}...")

    with open(dashboard_file) as f:
        dashboard = json.load(f)

    # Remove inputs that will be replaced by datasource UID
    dashboard.pop("__inputs", None)

    # Use existing Prometheus datasource UID
    payload = {
        "dashboard": dashboard,
        "overwrite": True,
        "folderId": 0,
    }

    resp = session.post(f"{GRAFANA_URL}/api/dashboards/db", json=payload)

    if resp.status_code in (200, 201):
        result = resp.json()
        print(f"  ✓ Imported: {dashboard.get('title', 'Unknown')}")
        print(f"    URL: {GRAFANA_URL}{result.get('url', '')}")
        return True
    else:
        print(f"  ✗ Failed: {resp.status_code} - {resp.text}")
        return False


def main() -> None:
    if len(sys.argv) > 1:
        password = sys.argv[1]
    else:
        # Check environment
        password = os.environ.get("GF_SECURITY_ADMIN_PASSWORD", "admin")

    print(f"Grafana URL: {GRAFANA_URL}")
    print(f"Using password: {'*' * len(password)}")

    # Create session with basic auth
    session = requests.Session()
    session.auth = ("admin", password)

    # Test connection
    try:
        resp = session.get(f"{GRAFANA_URL}/api/health")
        if resp.status_code != 200:
            print(f"Health check failed: {resp.status_code}")
            sys.exit(1)
        print(f"Grafana version: {resp.json().get('version', 'unknown')}")
    except requests.exceptions.ConnectionError:
        print(f"Cannot connect to Grafana at {GRAFANA_URL}")
        print("Make sure Grafana is running on port 3001")
        sys.exit(1)

    # Find dashboard files
    if not DASHBOARDS_DIR.exists():
        print(f"Dashboards directory not found: {DASHBOARDS_DIR}")
        sys.exit(1)

    dashboard_files = list(DASHBOARDS_DIR.glob("*.json"))
    if not dashboard_files:
        print(f"No dashboard JSON files found in {DASHBOARDS_DIR}")
        sys.exit(1)

    print(f"\nFound {len(dashboard_files)} dashboard(s)")

    success = 0
    for dashboard_file in dashboard_files:
        if import_dashboard(session, dashboard_file):
            success += 1

    print(f"\n✓ Imported {success}/{len(dashboard_files)} dashboard(s)")
    print(f"\nOpen Grafana at: {GRAFANA_URL}/grafana/")

    if success == 0:
        print("\n✗ Import failed. Check your password.")
        print("Usage: python import_dashboards.py <password>")
        print("   or: set GF_SECURITY_ADMIN_PASSWORD environment variable")


if __name__ == "__main__":
    main()
