#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

if [ ! -d ".venv" ]; then
  uv venv
fi

source .venv/bin/activate
uv pip install -e .

export GOSYSTEM_MCP_TRANSPORT="streamable-http"
export GOSYSTEM_MCP_HOST="${GOSYSTEM_MCP_HOST:-0.0.0.0}"
export GOSYSTEM_MCP_PORT="${GOSYSTEM_MCP_PORT:-8000}"
export GOSYSTEM_MCP_PATH="${GOSYSTEM_MCP_PATH:-/mcp}"
export GOSYSTEM_MCP_STATELESS_HTTP="${GOSYSTEM_MCP_STATELESS_HTTP:-true}"
export GOSYSTEM_MCP_JSON_RESPONSE="${GOSYSTEM_MCP_JSON_RESPONSE:-true}"

exec gosystem-test-mcp
