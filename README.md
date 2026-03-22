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

Arquivos:

- `docker-compose.yml` (arquivo padrao esperado pelo Dockploy)
- `Dockerfile`
- `.env.compose`

Rodar:

```bash
cp .env.compose .env
docker compose -p digital-solutions-test-mcp -f docker-compose.yml up -d --build --remove-orphans
```

## Configuracao (`config.toml`)

Campos principais:

- `[project].project_root`
- `[context].mode = "isolated"`
- `[context].store_root`
- `[context].developer_id`
- `[context].workspace_id`
- `[memory].chunk_chars`
- `[memory].chunk_overlap_chars`
- `[memory].default_max_chunks`
- `[memory].default_max_chars`

## Tools MCP

Core de testes:

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

1. `bootstrap_with_context`
2. `rag_index_context`
3. `rag_query` com pergunta objetiva antes de chamar a LLM externa
4. usar `context_compact` retornado no prompt da LLM
5. apos mudancas importantes, chamar `rag_index_context` novamente

## Isolamento de contexto

A memoria RAG e o estado ficam no contexto resolvido (`context_key`).
Com `context.mode = "isolated"`, cada projeto/workspace/dev enxerga apenas seu proprio contexto.

## CI/CD

Workflow: `.github/workflows/ci-cd.yml`

- CI em PR/push
- build/push de imagem em push para `main`/`master`
- tags de imagem automaticas: `latest`, `build-<run_number>`, `sha-<commit>`
