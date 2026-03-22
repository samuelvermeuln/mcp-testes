#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

if [ ! -d ".venv" ]; then
  uv venv
fi

source .venv/bin/activate
uv pip install -e .

CONFIG_PATH="${1:-$ROOT_DIR/config.toml}"
export GOSYSTEM_MCP_CONFIG_TOML="$CONFIG_PATH"

exec gosystem-test-mcp
