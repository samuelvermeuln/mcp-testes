# Test Metrics Log Template

Preencha uma linha por `TEST_CASE_ID`.

| TEST_CASE_ID | FEATURE | TEST_NAME | TYPE | COMPLEXITY | START_TIME_UTC | END_TIME_UTC | ACTUAL_MINUTES | BASELINE_MANUAL_MINUTES | SAVINGS_MINUTES | SAVINGS_PERCENT | PRODUCTIVITY_RATIO | STATUS | NOTES |
| --- | --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| TST-EXAMPLE-001 | Contracts | GetById_DeveRetornarEntidade_QuandoIdValido | repository | M | 2026-03-20T11:00:00Z | 2026-03-20T11:28:00Z | 28 | 45 | 17 | 37.78 | 1.61 | PASS | Incluiu validacao de include |

## Regras

- Horario sempre em UTC no formato ISO 8601.
- `ACTUAL_MINUTES` deve ser inteiro.
- `SAVINGS_MINUTES` pode ser negativo (quando IA demorou mais).
- Nao apagar historico; adicionar novas linhas.
