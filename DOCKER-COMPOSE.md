# Docker Compose (Local + Server)

## Local

1. Copie variaveis de ambiente:

```bash
cp .env.compose.example .env
```

2. Ajuste `PROJECTS_ROOT` e `MCP_DATA_DIR` no `.env`.

3. Suba com build local:

```bash
docker compose up --build -d
```

4. Verifique:

```bash
docker compose ps
```

O MCP ficara disponivel em `http://localhost:8000/mcp`.

## Servidor (imagem vinda do GHCR)

No servidor, rode sem override para evitar build:

```bash
docker compose -f compose.yaml pull
docker compose -f compose.yaml up -d --remove-orphans
```

## Deploy automatico e rollback

- Cada push na `main`/`master` gera tags automaticas no GHCR:
- `latest`
- `build-<run_number>`
- `sha-<commit>`
- No Dockploy, use `IMAGE_TAG` para escolher a versao.

Exemplo de rollback para `build-42`:

```bash
IMAGE_TAG=build-42 docker compose -f compose.yaml pull
IMAGE_TAG=build-42 docker compose -f compose.yaml up -d --remove-orphans
```

## Variaveis importantes

- `IMAGE_NAME` / `IMAGE_TAG`: imagem publicada no GHCR.
- `MCP_PORT`: porta publica do servidor.
- `GOSYSTEM_MCP_PATH`: path MCP HTTP (padrao `/mcp`).
- `GOSYSTEM_MCP_CONFIG_TOML`: arquivo TOML dentro do container.
- `PROJECTS_ROOT`: pasta com APIs .NET montadas no container.
