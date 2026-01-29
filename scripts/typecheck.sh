#!/usr/bin/env bash
set -euo pipefail

python -m mypy --config-file=pyproject.toml .
