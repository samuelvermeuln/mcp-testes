# Deployment and Client Connection (Production)

## 1) Local process (stdio)

```bash
cd /mnt/c/Users/samuelv/Documents/mcp-testes
source .venv/bin/activate
DIGITAL_SOLUTIONS_MCP_TRANSPORT=stdio \
DIGITAL_SOLUTIONS_MCP_CONFIG_TOML=/mnt/c/Users/samuelv/Documents/mcp-testes/config.toml \
python -m digital_solutions_test_mcp.server
```

## 2) Server process (SSE default)

```bash
cd /opt/digital-solutions-test-mcp
DIGITAL_SOLUTIONS_MCP_CONFIG_TOML=/opt/digital-solutions-test-mcp/config.toml \
python -m digital_solutions_test_mcp.server
```

Endpoints:

- Root diagnose: `http://<SERVER_IP>:8000/`
- MCP SSE: `http://<SERVER_IP>:8000/sse`
- SSE messages: `http://<SERVER_IP>:8000/messages/`
- Health: `http://<SERVER_IP>:8000/health`
- Streamable HTTP opcional: `http://<SERVER_IP>:8000/mcp` se `transport = "streamable-http"` no `config.toml`

Seguranca de host/origin:

- o arquivo `config.toml` libera hosts/origins externos por padrao para evitar `Invalid Host header`
- se quiser endurecer depois, ajuste `[server.security]` com dominios explicitos

## 3) Docker / Dockploy

Use `docker-compose.yml` no root do repositorio.

Deploy:

```bash
docker compose -p digital-solutions-test-mcp -f docker-compose.yml up -d --build --remove-orphans
```

No Dokploy/Traefik, a rota publica precisa apontar para a porta interna `8000` do service `gosystem-test-mcp`.
Se o dominio responder `404 page not found` em `/` e `/health`, o proxy ainda nao esta encaminhando para o container.
No `docker-compose.yml`, o service name permanece compativel com o app atual do Dokploy, mas o container publicado continua `digital-solutions-test-mcp`.

Sem configuracao por desenvolvedor:

- nao usa `.env.compose`
- nao usa `compose.yaml`/`compose.override.yaml`
- auto-detect de projeto habilitado (prioriza `/workspace/projects` quando `project_root` nao e enviado)
- paths Windows/Linux enviados pelo cliente podem ser remapeados para um projeto visivel no servidor pelo nome da pasta

Rollback por versao de imagem:

```bash
IMAGE_TAG=build-42 docker compose -p digital-solutions-test-mcp -f docker-compose.yml up -d --build --remove-orphans
```

Tags de release:

- cada push em `main`/`master` cria automaticamente uma nova tag semantica no GitHub
- isso permite rollback por tag anterior no repositorio, alem das tags de imagem no GHCR

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
codex mcp add digital-solutions-test-remote --url https://mcp.seudominio.com/sse
```

## 5) Codex config.toml snippet

```toml
[mcp_servers.digitalSolutionsTestMcp]
url = "https://mcp.seudominio.com/sse"
# bearer_token_env_var = "DIGITAL_SOLUTIONS_MCP_BEARER_TOKEN"
```

## 6) Claude Code

```bash
claude mcp remove mcp_solucoes_digitais_testes
claude mcp add --transport sse --scope user mcp_solucoes_digitais_testes "https://mcp.seudominio.com/sse"
```

Se o dominio publicar `404 page not found` em `/health` e `/`, o problema esta no roteamento do Traefik/Dokploy para a porta `8000`.

## 7) Multi-dev isolation

Para garantir que cada projeto veja apenas seu contexto, defina no `config.toml`:

- `[context].mode = "isolated"`
- `[context].store_root` compartilhado/persistente
- `[context].developer_id` por usuario
- `[context].workspace_id` por janela/workspace

A memoria RAG e persistida em SQLite por contexto e usada pelas tools `rag_*`.
