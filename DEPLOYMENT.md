# Deployment and Client Connection (Production)

## 1) Local process (stdio)

```bash
cd /mnt/c/Users/samuelv/Documents/mcp-testes
source .venv/bin/activate
DIGITAL_SOLUTIONS_MCP_TRANSPORT=stdio \
DIGITAL_SOLUTIONS_MCP_CONFIG_TOML=/mnt/c/Users/samuelv/Documents/mcp-testes/config.toml \
python -m digital_solutions_test_mcp.server
```

## 2) Server process (streamable-http)

```bash
cd /opt/digital-solutions-test-mcp
DIGITAL_SOLUTIONS_MCP_TRANSPORT=streamable-http \
DIGITAL_SOLUTIONS_MCP_HOST=0.0.0.0 \
DIGITAL_SOLUTIONS_MCP_PORT=8000 \
DIGITAL_SOLUTIONS_MCP_PATH=/mcp \
DIGITAL_SOLUTIONS_MCP_CONFIG_TOML=/opt/digital-solutions-test-mcp/config.toml \
python -m digital_solutions_test_mcp.server
```

Endpoints:

- MCP: `http://<SERVER_IP>:8000/mcp`
- Health: `http://<SERVER_IP>:8000/health`

## 3) Docker / Dockploy

Use `docker-compose.yml` no root do repositorio.

Deploy:

```bash
docker compose -p digital-solutions-test-mcp -f docker-compose.yml up -d --build --remove-orphans
```

Rollback por versao de imagem:

```bash
IMAGE_TAG=build-42 docker compose -p digital-solutions-test-mcp -f docker-compose.yml up -d --build --remove-orphans
```

## 4) Codex CLI connection

Local stdio:

```bash
codex mcp add digital-solutions-test-local \
  --env DIGITAL_SOLUTIONS_MCP_TRANSPORT=stdio \
  --env DIGITAL_SOLUTIONS_MCP_CONFIG_TOML=/mnt/c/Users/samuelv/Documents/mcp-testes/config.toml \
  -- python -m digital_solutions_test_mcp.server
```

Remote HTTP:

```bash
codex mcp add digital-solutions-test-remote --url https://mcp.seudominio.com/mcp
```

## 5) Codex config.toml snippet

```toml
[mcp_servers.digitalSolutionsTestMcp]
url = "https://mcp.seudominio.com/mcp"
# bearer_token_env_var = "DIGITAL_SOLUTIONS_MCP_BEARER_TOKEN"
```

## 6) Multi-dev isolation

Para garantir que cada projeto veja apenas seu contexto, defina no `config.toml`:

- `[context].mode = "isolated"`
- `[context].store_root` compartilhado/persistente
- `[context].developer_id` por usuario
- `[context].workspace_id` por janela/workspace

A memoria RAG e persistida em SQLite por contexto e usada pelas tools `rag_*`.
