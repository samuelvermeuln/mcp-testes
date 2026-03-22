#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

if [ ! -d ".venv" ]; then
  uv venv
fi

source .venv/bin/activate
uv pip install -e .

exec gosystem-test-mcp
