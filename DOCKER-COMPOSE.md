# Docker Compose (Production)

Dockploy usa `docker-compose.yml` por padrao.

## 1) Subir servico

```bash
docker compose -p digital-solutions-test-mcp -f docker-compose.yml up -d --build --remove-orphans
```

## 2) Endpoints

- Root diagnose: `http://localhost:8000/`
- MCP SSE: `http://localhost:8000/sse`
- SSE messages: `http://localhost:8000/messages/`
- Health: `http://localhost:8000/health`

## 3) Rollback

```bash
IMAGE_TAG=build-42 docker compose -p digital-solutions-test-mcp -f docker-compose.yml up -d --build --remove-orphans
```

## 4) Comportamento sem variaveis por dev

- sem arquivo `.env.compose`
- sem `compose.yaml` e sem `compose.override.yaml`
- o servidor usa volumes nomeados internos para estado e workspace
- se `project_root` nao for informado, o MCP tenta identificar automaticamente projetos em `/workspace/projects`
- o compose nao sobrescreve o transporte do `config.toml`; default atual e `sse`
