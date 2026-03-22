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
DIGITAL_SOLUTIONS_MCP_TRANSPORT=streamable-http \
DIGITAL_SOLUTIONS_MCP_HOST=0.0.0.0 \
DIGITAL_SOLUTIONS_MCP_PORT=8000 \
DIGITAL_SOLUTIONS_MCP_PATH=/mcp \
digital-solutions-test-mcp
```

Endpoints:

- MCP: `http://<host>:8000/mcp`
- Health: `http://<host>:8000/health`

## Docker / Dockploy

Rodar:

```bash
docker compose -p digital-solutions-test-mcp -f docker-compose.yml up -d --build --remove-orphans
```

Observacoes:

- existe somente `docker-compose.yml` (sem `compose.yaml`/`override` e sem `.env.compose`)
- o servidor usa volume persistente para dados e para workspace de projetos
- `project_root` pode ser omitido: o MCP tenta detectar automaticamente em `/workspace/projects`

## Configuracao (`config.toml`)

Campos principais:

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

## Tools MCP

Core de testes:

- `route_project` (resolve projeto por intent e fixa contexto ativo)
- `get_active_project`
- `clear_active_project`
- `detect_project`
- `bootstrap`
- `bootstrap_with_context`
- `discover_test_targets`
- `generate_tests`
- `validate`
- `coverage_gate`
- `pipeline`
- `start_timer`
- `stop_timer`
- `metrics_summary`
- `resolve_context`
- `list_contexts`
- `get_runtime_settings`

Memoria/RAG (token optimization):

- `rag_index_context`: indexa estado/contexto no SQLite
- `rag_upsert_note`: grava memoria manual por `source`
- `rag_query`: recupera contexto compacto por relevancia
- `rag_stats`: visao de ocupacao/tokens estimados

## Fluxo recomendado para baixo consumo de token

1. `route_project` (passar `intent` e IDs de contexto/dev/workspace)
2. `rag_query` com pergunta objetiva antes de chamar a LLM externa
3. usar `context_compact` retornado no prompt da LLM
4. apos mudancas importantes, chamar `rag_index_context` novamente
5. ao trocar de API, chamar `route_project` novamente com novo `intent`

## Selecao inteligente de projeto

- o MCP guarda projeto ativo por identidade (`context_id` ou `developer_id+workspace_id`)
- na primeira vez, seleciona por:
  - `project_root` manual (se enviado), ou
  - LLM (OpenAI/Anthropic/comando externo), ou
  - fallback heuristico local
- nas proximas chamadas, reutiliza o projeto em cache
- se contexto/estado sumir, o MCP recria bootstrap e reindexa RAG automaticamente

Variaveis de ambiente do roteador (opcionais):

- `DIGITAL_SOLUTIONS_ROUTER_PREFER_LLM=true|false`
- `DIGITAL_SOLUTIONS_ROUTER_PROVIDER=openai|anthropic`
- `DIGITAL_SOLUTIONS_ROUTER_MODEL=<modelo>`
- `DIGITAL_SOLUTIONS_ROUTER_COMMAND="<comando externo>"`
- `DIGITAL_SOLUTIONS_PROJECTS_ROOT=/workspace/projects`

## Isolamento de contexto

A memoria RAG e o estado ficam no contexto resolvido (`context_key`).
Com `context.mode = "isolated"`, cada projeto/workspace/dev enxerga apenas seu proprio contexto.

## CI/CD

Workflow: `.github/workflows/ci-cd.yml`

- CI em PR/push
- build/push de imagem em push para `main`/`master`
- tags de imagem automaticas: `latest`, `build-<run_number>`, `sha-<commit>`
