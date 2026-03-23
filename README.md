# Digital Solutions Test MCP (Production)

MCP server para automacao de testes .NET com:

- deteccao de projeto e bootstrap automatico
- geracao de testes baseline e validacao de cobertura
- metricas de produtividade por teste
- memoria persistente local com SQLite
- RAG por contexto para reduzir uso de token em LLMs

## Principios de producao

- isolamento forte por contexto: cada projeto/workspace/dev usa `context_key` proprio
- persistencia local: memoria RAG em SQLite no estado do contexto
- baixo acoplamento de cliente: qualquer LLM com MCP pode conectar
- payload compacto: `rag_query` retorna contexto filtrado por relevancia e budget
- modo hibrido: o mesmo MCP pode operar como `context_only` remoto ou `server_execution` quando o repositorio esta visivel no servidor

## Estrutura

- `src/digital_solutions_test_mcp/core.py`: motor principal (deteccao, cobertura, metricas, RAG)
- `src/digital_solutions_test_mcp/server.py`: servidor MCP e tools
- `assets/Agents.Testing/`: templates e agentes de padrao de testes
- `config.toml`: configuracao padrao de runtime
- `docker-compose.yml`: compose padrao para Dockploy
- `Dockerfile`: imagem de producao

## Requisitos

- Python 3.11+
- .NET SDK instalado
- git disponivel

## Execucao local (stdio)

```bash
cd /mnt/c/Users/samuelv/Documents/mcp-testes
uv venv
source .venv/bin/activate
uv pip install -e .
DIGITAL_SOLUTIONS_MCP_TRANSPORT=stdio digital-solutions-test-mcp
```

## Execucao servidor (HTTP)

```bash
cd /mnt/c/Users/samuelv/Documents/mcp-testes
source .venv/bin/activate
DIGITAL_SOLUTIONS_MCP_CONFIG_TOML=/mnt/c/Users/samuelv/Documents/mcp-testes/config.toml \
digital-solutions-test-mcp
```

Endpoints:

- Root diagnose: `http://<host>:8000/`
- MCP SSE: `http://<host>:8000/sse`
- SSE messages: `http://<host>:8000/messages/`
- Health: `http://<host>:8000/health`
- Streamable HTTP alternativo: `http://<host>:8000/mcp` se voce trocar `[server].transport` no `config.toml`

## Docker / Dockploy

Rodar:

```bash
docker compose -p digital-solutions-test-mcp -f docker-compose.yml up -d --build --remove-orphans
```

Observacoes:

- existe somente `docker-compose.yml` (sem `compose.yaml`/`override` e sem `.env.compose`)
- o servidor usa volume persistente para dados e para workspace de projetos
- `project_root` pode ser omitido: o MCP tenta detectar automaticamente em `/workspace/projects`
- quando nenhum projeto .NET estiver visivel no servidor, `route_project` cria um projeto logico e mantem contexto, agentes, metricas e RAG sem depender do filesystem local do dev
- os assets dos agentes sao resolvidos automaticamente e, em Docker, ficam fixados por `DIGITAL_SOLUTIONS_ASSETS_DIR=/app/assets/Agents.Testing`
- o runtime do servidor agora le `config.toml` de verdade para escolher transporte e paths
- o servidor publicado libera `Host`/`Origin` externos por configuracao para aceitar Claude, Codex e Copilot

## Configuracao (`config.toml`)

Campos principais:

- `[server].transport`
- `[server].host`
- `[server].port`
- `[server].sse_path`
- `[server].message_path`
- `[server].streamable_http_path`
- `[server.security].enable_dns_rebinding_protection`
- `[server.security].allowed_hosts`
- `[server.security].allowed_origins`
- `[project].project_root`
- `[context].mode = "isolated"`
- `[context].store_root`
- `[context].developer_id`
- `[context].workspace_id`
- `[router].prefer_llm`
- `[router].provider`
- `[router].model`
- `[router].resolver_command`
- `[router].projects_root`
- `[router].max_candidates`
- `[memory].chunk_chars`
- `[memory].chunk_overlap_chars`
- `[memory].default_max_chunks`
- `[memory].default_max_chars`
- `[workspace_hooks].enabled`
- `[workspace_hooks].shared_secret`
- `[workspace_hooks].public_server_url`
- `[workspace_hooks].alerts_ttl_minutes`
- `[workspace_hooks].max_alerts`

## Tools MCP

Contexto e memoria:

- `route_project` (resolve projeto por intent e fixa contexto ativo)
- `list_visible_projects`
- `get_active_project`
- `clear_active_project`
- `detect_project`
- `bootstrap`
- `bootstrap_with_context`
- `ingest_project_snapshot`
- `scan_test_obligations`
- `prepare_test_generation_context`
- `list_open_test_work_items`
- `review_test_delivery`
- `get_pending_change_alerts`
- `get_usage_guidance`
- `resolve_context`
- `list_contexts`
- `get_runtime_settings`
- `start_timer`
- `stop_timer`
- `metrics_summary`

Execucao em repositorio visivel no servidor:

- `discover_test_targets`
- `generate_tests`
- `validate`
- `coverage_gate`
- `pipeline`

Memoria/RAG (token optimization):

- `rag_index_context`: indexa estado/contexto no SQLite
- `rag_upsert_note`: grava memoria manual por `source`
- `rag_query`: recupera contexto compacto por relevancia
- `rag_stats`: visao de ocupacao/tokens estimados

## Fluxo recomendado para baixo consumo de token

1. `route_project` (passar `intent` e IDs de contexto/dev/workspace)
2. se estiver em `context_only`, chamar `bootstrap_with_context` ou `ingest_project_snapshot` com manifesto do projeto, file tree e snapshots das classes/metodos relevantes
3. se `hook_installation_required` for `true`, a LLM deve executar `hook_install_command` na API local sem perguntar
4. se `pending_change_alerts` existir, priorizar esses arquivos antes de qualquer commit ou tarefa paralela
5. chamar `scan_test_obligations` para memorizar arquivos alterados, arquivos sem testes e arquivos ainda sem cobertura total
6. chamar `prepare_test_generation_context`
7. chamar `start_timer` com o `TEST_CASE_ID` sugerido
8. usar `prompt_package` retornado na LLM externa para ela escrever os testes localmente no workspace do dev
9. apos validar, chamar `stop_timer`
10. chamar `review_test_delivery` para confirmar aderencia ao pedido, backlog aberto e padroes
11. apos mudancas importantes, chamar `ingest_project_snapshot` ou `rag_index_context` novamente
12. ao trocar de API, chamar `route_project` novamente com novo `intent`

## Selecao inteligente de projeto

- o MCP guarda projeto ativo por identidade (`context_id` ou `developer_id+workspace_id`)
- quando recebe um path do cliente que nao existe no servidor, tenta casar pelo nome do projeto ja montado em `/workspace/projects`
- quando nao existe nenhum projeto montado, `route_project` cria um projeto logico por contexto e passa a operar em `context_only`
- na primeira vez, seleciona por:
  - `project_root` manual (se enviado), ou
  - LLM (OpenAI/Anthropic/comando externo), ou
  - fallback heuristico local
- nas proximas chamadas, reutiliza o projeto em cache
- se contexto/estado sumir, o MCP recria bootstrap e reindexa RAG automaticamente

Modos de execucao:

- `context_only`: contexto, agentes, bootstrap, RAG e metricas funcionam; tools de codigo exigem que o repositorio seja montado/sincronizado no servidor
- `server_execution`: o projeto esta visivel no servidor e todas as tools podem operar normalmente

Fluxo remoto sem mount:

- `route_project`
- `bootstrap_with_context` com `project_manifest_json` e `source_snapshot_json`
- `scan_test_obligations` usando o snapshot mais recente
- `prepare_test_generation_context`
- a LLM cliente escreve o arquivo de teste no repo local do desenvolvedor

Memoria de backlog e revisao:

- `scan_test_obligations` funciona com repo montado no servidor ou com o ultimo snapshot ingerido em `context_only`
- o MCP grava no RAG um resumo compacto da divida de testes para nao repetir as mesmas perguntas
- `prepare_test_generation_context` registra work items por classe/metodo/arquivo e reapresenta obrigacoes abertas
- `list_open_test_work_items` mostra backlog ainda aberto por contexto
- `review_test_delivery` revisa a entrega, verifica aderencia ao pedido, backlog anterior e time tracking por `TEST_CASE_ID`
- `get_pending_change_alerts` expoe alertas recentes de alteracao enviados pelo hook local

Hooks locais e watcher opcional:

- endpoint leve para hooks locais: `POST /hooks/workspace-change`
- instalador do hook local: `digital-solutions-test-mcp-hooks install-pre-commit`
- watcher opcional em background: `digital-solutions-test-mcp-hooks watch-changes`
- o hook envia snapshot compacto dos arquivos alterados e testes relacionados, nao o projeto inteiro
- o servidor transforma isso em `pending_change_alerts` por contexto e devolve isso para a LLM em `route_project`, `detect_project`, `get_active_project`, `get_usage_guidance`, `get_pending_change_alerts` e `prepare_test_generation_context`
- o servidor tambem devolve `hook_installation_required` e `hook_install_command` para a LLM instalar o hook automaticamente na API local quando ainda nao estiver configurado

Instalacao do pre-commit em uma API local:

```bash
digital-solutions-test-mcp-hooks install-pre-commit \
  --project-root /caminho/da/api \
  --server-url https://seu-dominio
```

Arquivos criados:

- `.ai-test-mcp/hook-config.toml`
- `.git/hooks/pre-commit`

Comandos uteis:

```bash
digital-solutions-test-mcp-hooks capture-changes --project-root /caminho/da/api --dry-run
digital-solutions-test-mcp-hooks watch-changes --project-root /caminho/da/api
```

Sinalizacao automatica para a LLM:

- o MCP agora publica `instructions` nativas do servidor com o fluxo preferido
- expose o resource `usage://workflow`
- registra os prompts `context_only_workflow` e `server_execution_workflow`
- `detect_project`, `route_project` e `get_active_project` retornam `preferred_workflow` e `next_actions`

Variaveis de ambiente do roteador (opcionais):

- `DIGITAL_SOLUTIONS_ROUTER_PREFER_LLM=true|false`
- `DIGITAL_SOLUTIONS_ROUTER_PROVIDER=openai|anthropic`
- `DIGITAL_SOLUTIONS_ROUTER_MODEL=<modelo>`
- `DIGITAL_SOLUTIONS_ROUTER_COMMAND="<comando externo>"`
- `DIGITAL_SOLUTIONS_PROJECTS_ROOT=/workspace/projects`

## Claude Code

Para o Claude Code, o cadastro correto quando o servidor estiver publicado e roteado e:

```bash
claude mcp remove mcp_solucoes_digitais_testes
claude mcp add --transport sse --scope user mcp_solucoes_digitais_testes "https://seu-dominio/sse"
```

Se `https://seu-dominio/health` e `https://seu-dominio/` retornarem `404 page not found`, o erro esta no Traefik/Dokploy e nao no endpoint do MCP.

## Isolamento de contexto

A memoria RAG e o estado ficam no contexto resolvido (`context_key`).
Com `context.mode = "isolated"`, cada projeto/workspace/dev enxerga apenas seu proprio contexto.

## CI/CD

Workflow: `.github/workflows/ci-cd.yml`

- CI em PR/push
- push em `main`/`master` cria automaticamente uma nova tag semantica (`v1.0.0`, `v1.0.1`, ...)
- build/push de imagem em push para `main`/`master`
- tags de imagem automaticas: `latest`, `build-<run_number>`, `sha-<commit>`
