#!/usr/bin/env bash
set -euo pipefail

uv run mypy --config-file=pyproject.toml .
