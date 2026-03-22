# Gosystem Test MCP (Local)

MCP server local para automacao de testes C#/.NET com foco em:

- deteccao automatica de projeto e variaveis
- sincronizacao de agentes de qualidade de teste
- descoberta de classes/metodos alterados que precisam de teste
- geracao automatica de testes baseline
- validacao de build/test/cobertura
- metrica de tempo por teste e economia com IA

## Estrutura

- `assets/Agents.Testing/`: agentes e templates de testes/metricas
- `config.toml.example`: exemplo de configuracao TOML para codex-as-mcp
- `DEPLOYMENT.md`: guia de deploy em servidor e conexao de clientes MCP
- `src/gosystem_test_mcp/core.py`: motor de deteccao/geracao/cobertura/metricas
- `src/gosystem_test_mcp/server.py`: servidor MCP e tools
- `scripts/`: scripts para execucao local

## Requisitos

- Python 3.11+
- `dotnet` SDK instalado
- `uv` instalado (recomendado)

## Setup rapido (Windows/WSL)

```bash
cd /mnt/c/Users/samuelv/RiderProjects/gosystem-test-mcp
uv venv
source .venv/bin/activate
uv pip install -e .
```

No PowerShell:

```powershell
cd C:\Users\samuelv\RiderProjects\gosystem-test-mcp
uv venv
.\.venv\Scripts\Activate.ps1
uv pip install -e .
```

## Rodar servidor MCP (modo simples)

```bash
cd /mnt/c/Users/samuelv/RiderProjects/gosystem-test-mcp
source .venv/bin/activate
gosystem-test-mcp
```

## Configuracao via TOML (codex-as-mcp)

1. Copie `config.toml.example` para `config.toml`.
2. Ajuste `project.project_root`, `context.store_root`, `context.developer_id` e `context.workspace_id`.
3. Rode com TOML:

```bash
cd /mnt/c/Users/samuelv/RiderProjects/gosystem-test-mcp
./scripts/run-local-with-toml.sh ./config.toml
```

No Windows:

```cmd
scripts\\run-local-with-toml.cmd C:\\Users\\samuelv\\RiderProjects\\gosystem-test-mcp\\config.toml
```

Com TOML, as tools podem ser chamadas sem `project_root`, porque o MCP usa `[project].project_root`.

Exemplos de `config.toml` do Codex por janela:

- `examples/codex.mobility-api.config.toml`
- `examples/codex.mobility-app-api.config.toml`

Trecho base (formato oficial `config.toml`):

```toml
[mcp_servers.gosystemTestMcp]
command = "C:/Users/samuelv/RiderProjects/gosystem-test-mcp/.venv/Scripts/python.exe"
args = ["-m", "gosystem_test_mcp.server"]
cwd = "C:/Users/samuelv/RiderProjects/gosystem-test-mcp"
startup_timeout_sec = 30.0
tool_timeout_sec = 180.0
env = { GOSYSTEM_MCP_TRANSPORT = "stdio", GOSYSTEM_MCP_CONFIG_TOML = "C:/Users/samuelv/RiderProjects/gosystem-test-mcp/examples/mobility-api.context.toml" }
```

## Tools MCP disponiveis

- `detect_project(project_root?, config_toml_path?)`
- `bootstrap(project_root?, overwrite_agents=false, config_toml_path?)`
- `bootstrap_with_context(project_root?, ..., config_toml_path?)`
- `resolve_context(project_root?, ..., config_toml_path?)`
- `list_contexts(context_root?, config_toml_path?)`
- `get_runtime_settings(project_root?, config_toml_path?)`
- `discover_test_targets(project_root?, base_ref="HEAD~1", include_untracked=true, config_toml_path?)`
- `generate_tests(project_root?, base_ref="HEAD~1", dry_run=false, config_toml_path?)`
- `validate(project_root?, run_coverage=true, configuration="Debug", ..., config_toml_path?)`
- `coverage_gate(project_root?, base_ref="HEAD~1", min_line_rate=1.0, ..., config_toml_path?)`
- `pipeline(project_root?, base_ref="HEAD~1", min_line_rate=1.0, ..., config_toml_path?)`
- `start_timer(...)`
- `stop_timer(...)`
- `metrics_summary(project_root?, ..., config_toml_path?)`
- `list_agent_files()`
- `get_agent_file(file_name)`

## Fluxo recomendado por API

1. `bootstrap`
2. `discover_test_targets`
3. `generate_tests`
4. `validate`
5. `coverage_gate` com `min_line_rate=1.0`

## Rodar em duas APIs

Use um TOML por janela/workspace (cada um com `project_root` e `workspace_id` proprios), por exemplo:

- `examples/mobility-api.context.toml`
- `examples/mobility-app-api.context.toml`

Com `context.mode = "isolated"` e `context.store_root`, cada dev/janela ganha contexto unico.
Isso evita mistura de estado entre projetos e entre desenvolvedores.

Padrao de isolamento recomendado:

- `context.developer_id`: identifica a pessoa
- `context.workspace_id`: identifica a janela/workspace
- `context.store_root`: repositorio central de contextos compartilhavel entre time

Script pronto para bootstrap rapido em duas APIs:

```bash
cd /mnt/c/Users/samuelv/RiderProjects/gosystem-test-mcp
source .venv/bin/activate
python scripts/bootstrap-two-apis.py \
  /mnt/c/Users/samuelv/RiderProjects/gosystem-mobility-api \
  /mnt/c/Users/samuelv/RiderProjects/gosystem-mobility-app-api
```

## Saidas geradas automaticamente por API

No contexto resolvido (local ou em `context.store_root`):

- `.ai-test-mcp/project-profile.json`
- `.ai-test-mcp/variables.json`
- `.ai-test-mcp/agents/*.md`
- `.ai-test-mcp/metrics/test-metrics-log.md`
- `.ai-test-mcp/metrics/ai-savings-report.md`
- `.ai-test-mcp/metrics/timers.json`

## Observacao importante sobre cobertura completa

O servidor aplica gate de cobertura por arquivo alterado. Para garantia efetiva de 100%, rode `coverage_gate` com `min_line_rate=1.0` e trate os arquivos que falharem ate zerar pendencias.

## Docker Compose

Arquivos adicionados para containerizacao:

- `Dockerfile`
- `compose.yaml`
- `compose.override.yaml`
- `.env.compose.example`
- `DOCKER-COMPOSE.md`

Executar local:

```bash
cp .env.compose.example .env
docker compose up --build -d
```

MCP HTTP: `http://localhost:8000/mcp`

## CI/CD GitHub Actions

Workflow: `.github/workflows/ci-cd.yml`

- CI: instala pacote, compila `core.py` e `server.py`, smoke test de import.
- CD: build/push da imagem no GHCR (`ghcr.io/<owner>/<repo>`).
- Deploy opcional via SSH + Docker Compose, habilitado por secrets.
