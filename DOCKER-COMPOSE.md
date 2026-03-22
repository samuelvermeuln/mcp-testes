# Docker Compose (Production)

Dockploy usa `docker-compose.yml` por padrao.

## 1) Preparar ambiente

```bash
cp .env.compose .env
```

Revise no `.env`:

- `IMAGE_NAME`
- `IMAGE_TAG`
- `MCP_PORT`
- `MCP_DATA_DIR`
- `PROJECTS_ROOT`

## 2) Subir servico

```bash
docker compose -p digital-solutions-test-mcp -f docker-compose.yml up -d --build --remove-orphans
```

## 3) Endpoints

- MCP: `http://localhost:8000/mcp`
- Health: `http://localhost:8000/health`

## 4) Rollback

```bash
IMAGE_TAG=build-42 docker compose -p digital-solutions-test-mcp -f docker-compose.yml up -d --build --remove-orphans
```

## 5) Variaveis importantes

- `IMAGE_NAME` / `IMAGE_TAG`: imagem publicada no GHCR
- `MCP_PORT`: porta publica do MCP
- `DIGITAL_SOLUTIONS_MCP_PATH`: path HTTP do MCP (`/mcp`)
- `DIGITAL_SOLUTIONS_MCP_CONFIG_TOML`: caminho do TOML no container
- `PROJECTS_ROOT`: raiz com APIs .NET montadas no container
