# Deploy and Connect (Codex, VSCode, other LLMs)

## 1) Local mode (stdio) - fastest start

Run:

```bash
cd /mnt/c/Users/samuelv/RiderProjects/gosystem-test-mcp
./scripts/run-local-with-toml.sh ./examples/mobility-api.context.toml
```

Codex connection (one-time):

```bash
codex mcp add gosystem-test-local --env GOSYSTEM_MCP_CONFIG_TOML=/mnt/c/Users/samuelv/RiderProjects/gosystem-test-mcp/examples/mobility-api.context.toml -- python -m gosystem_test_mcp.server
```

## 2) Server mode (streamable-http) - multi developers

### Linux server startup

```bash
cd /opt/gosystem-test-mcp
./scripts/run-http.sh
```

Default endpoint:

- `http://<SERVER_IP>:8000/mcp`

Recommended: expose via HTTPS reverse proxy (`examples/nginx.gosystem-test-mcp.conf`).

## 3) Codex CLI connection to remote MCP

```bash
codex mcp add gosystem-test-remote --url https://mcp.seudominio.com/mcp
```

If protected by bearer token:

```bash
export GOSYSTEM_MCP_BEARER_TOKEN="<token>"
codex mcp add gosystem-test-remote --url https://mcp.seudominio.com/mcp --bearer-token-env-var GOSYSTEM_MCP_BEARER_TOKEN
```

## 4) Codex via config.toml (manual)

```toml
[mcp_servers.gosystemTestMcp]
url = "https://mcp.seudominio.com/mcp"
# optional when auth is required:
# bearer_token_env_var = "GOSYSTEM_MCP_BEARER_TOKEN"
```

For local stdio:

```toml
[mcp_servers.gosystemTestMcp]
command = "C:/Users/samuelv/RiderProjects/gosystem-test-mcp/.venv/Scripts/python.exe"
args = ["-m", "gosystem_test_mcp.server"]

[mcp_servers.gosystemTestMcp.env]
GOSYSTEM_MCP_CONFIG_TOML = "C:/Users/samuelv/RiderProjects/gosystem-test-mcp/examples/mobility-api.context.toml"
GOSYSTEM_MCP_TRANSPORT = "stdio"
```

## 5) VSCode + other LLM clients

Any MCP client that supports one of these transports can connect:

- `stdio` (local process launch)
- `streamable-http` (remote URL)

Use one context TOML per workspace/window and set:

- `project_root`
- `developer_id`
- `workspace_id`
- `store_root`

This guarantees isolated context per developer and per project.
