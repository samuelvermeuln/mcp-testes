# Agents.Testing - Kit de Agentes para Testes C#

## Objetivo

Padronizar a criacao, revisao e medicao de testes C# com agentes, com foco em:

- qualidade tecnica rigorosa
- rastreabilidade completa
- execucao reutilizavel em outras APIs .NET
- medicao de tempo por teste e economia real com IA

## Escopo

Este kit foi desenhado para projetos .NET 8 com xUnit e coverlet, mas pode ser reutilizado em qualquer API C# ajustando os parametros de configuracao.

## Parametros que devem ser preenchidos por projeto

Preencha estes campos antes da primeira execucao:

- `PROJECT_NAME`: nome da API/produto
- `SOLUTION_PATH`: caminho da solucao `.sln`
- `TEST_PROJECT_PATH`: caminho do projeto de testes `.csproj`
- `TEST_FRAMEWORK`: `xUnit` (ou outro, se adaptar regras)
- `DOTNET_VERSION`: ex: `net8.0`
- `COVERAGE_SETTINGS_PATH`: caminho do arquivo `.runsettings`/coverlet settings
- `COVERAGE_LINE_TARGET`: meta minima de linha (ex: `80`)
- `COVERAGE_BRANCH_TARGET`: meta minima de branch (ex: `70`)
- `METRICS_LOG_PATH`: caminho do log de metricas em markdown
- `SAVINGS_REPORT_PATH`: caminho do relatorio final de economia

## Estrutura de agentes

- `ORCHESTRATOR.md`: agente mestre que coordena o fluxo completo
- `TEST-STANDARDS-AGENT.md`: guardiao de padroes rigorosos de teste
- `TEST-WRITER-AGENT.md`: agente implementador de testes
- `TEST-REVIEWER-AGENT.md`: agente revisor tecnico e gatekeeper
- `TEST-METRICS-AGENT.md`: agente de medicao e economia de tempo
- `TIME-TRACKING-PROTOCOL.md`: protocolo operacional de medicao por teste

## Fluxo minimo recomendado

1. Executar `ORCHESTRATOR.md`.
2. O orquestrador cria backlog de testes e delega para os agentes.
3. O writer implementa testes por lote pequeno.
4. O reviewer valida qualidade, regressao e flakiness.
5. O metrics agent registra tempo por teste e consolida economia.
6. O orquestrador aplica o `TIME-TRACKING-PROTOCOL.md` em todos os testes.
7. O orquestrador publica relatorio final com gaps e proximos passos.

## Artefatos esperados por rodada

- patch dos testes criados/ajustados
- lista de comandos executados
- resultado de build/test
- log de metricas por teste
- relatorio consolidado de economia com IA

## Referencias de evidencia usadas no padrao

- xUnit docs (Fact/Theory, async Task, MemberData estavel):
  - https://github.com/xunit/xunit.net/tree/main/site/docs
- xUnit analyzers (async/assertions):
  - https://github.com/xunit/xunit.net/tree/main/site/xunit.analyzers/rules
- Coverlet collector e cobertura com `dotnet test`:
  - https://github.com/coverlet-coverage/coverlet

## Observacao importante

Este kit assume que os agentes nao inventam APIs, nao escondem falhas e sempre reportam lacunas documentais com clareza.
