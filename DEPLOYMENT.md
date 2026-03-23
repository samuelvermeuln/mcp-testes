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

Modos de operacao:

- `context_only`: o cliente conecta no MCP remoto para obter regras, agentes, RAG, memoria e metricas sem expor o repo do dev ao servidor
- `server_execution`: alem do contexto, o servidor enxerga o repositorio montado e pode rodar deteccao de mudancas, gerar testes, validar build/teste e coverage

Fluxo recomendado para `context_only`:

- `route_project`
- `bootstrap_with_context` ou `ingest_project_snapshot` enviando manifesto, file tree e snapshots das classes/metodos relevantes
- `scan_test_obligations`
- `prepare_test_generation_context`
- a LLM cliente escreve/edita os testes localmente no workspace do desenvolvedor
- `stop_timer`
- `review_test_delivery`

Sinalizacao para clientes MCP:

- o servidor publica `instructions` com esse fluxo como comportamento padrao
- o resource `usage://workflow` expõe a mesma orientacao
- os prompts `context_only_workflow` e `server_execution_workflow` podem ser lidos pelo cliente
- `pending_change_alerts` sao devolvidos para a LLM quando hooks locais ou watcher opcional detectam alteracoes recentes

Workspace hooks:

- endpoint HTTP: `POST /hooks/workspace-change`
- endpoint de sync de branch: `POST /hooks/workspace-branch-state`
- endpoint de registro: `POST /hooks/register-workspace-hook`
- configuracao no servidor: `[workspace_hooks]` em `config.toml`
- para o MCP devolver `hook_install_command` pronto para a LLM, configure `[workspace_hooks].public_server_url`
- se `shared_secret` for definido, o hook local deve enviar o mesmo valor no header `X-Digital-Solutions-Hook-Secret`
- o instalador local cria `pre-commit`, `post-checkout` e `post-merge`, entao alteracoes e mudancas de branch entram no contexto sem depender de perguntas extras da LLM

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
- se nenhum projeto estiver montado, `route_project` cria um projeto logico isolado para manter contexto e RAG; para tools de execucao, continue montando/sincronizando o repo em `/workspace/projects`
- em ambiente local sem permissao de escrita em `/data`, o MCP usa fallback automatico para `.ai-test-mcp/_projects`
- os assets dos agentes ficam explicitamente configurados por `DIGITAL_SOLUTIONS_ASSETS_DIR=/app/assets/Agents.Testing`

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
