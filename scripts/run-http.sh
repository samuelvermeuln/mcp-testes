#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

if [ ! -d ".venv" ]; then
  uv venv
fi

source .venv/bin/activate
uv pip install -e .

export DIGITAL_SOLUTIONS_MCP_TRANSPORT="streamable-http"
export DIGITAL_SOLUTIONS_MCP_HOST="${DIGITAL_SOLUTIONS_MCP_HOST:-0.0.0.0}"
export DIGITAL_SOLUTIONS_MCP_PORT="${DIGITAL_SOLUTIONS_MCP_PORT:-8000}"
export DIGITAL_SOLUTIONS_MCP_PATH="${DIGITAL_SOLUTIONS_MCP_PATH:-/mcp}"
export DIGITAL_SOLUTIONS_MCP_STATELESS_HTTP="${DIGITAL_SOLUTIONS_MCP_STATELESS_HTTP:-true}"
export DIGITAL_SOLUTIONS_MCP_JSON_RESPONSE="${DIGITAL_SOLUTIONS_MCP_JSON_RESPONSE:-true}"

exec digital-solutions-test-mcp
