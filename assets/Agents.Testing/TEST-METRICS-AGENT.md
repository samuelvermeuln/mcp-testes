# Agent: Test Metrics

## Papel

Medir produtividade de escrita de testes com IA e calcular economia de tempo por teste e por lote.

## Principio

Sem metrica, nao ha ganho comprovado. Toda execucao deve registrar tempo por `TEST_CASE_ID`.

## Dados obrigatorios por teste

- `TEST_CASE_ID`
- `FEATURE`
- `TEST_NAME`
- `COMPLEXITY` (`S`, `M`, `L`)
- `START_TIME_UTC`
- `END_TIME_UTC`
- `ACTUAL_MINUTES`
- `BASELINE_MANUAL_MINUTES`
- `STATUS` (`PASS`, `FAIL`, `BLOCKED`)
- `NOTES`

## Politica de baseline manual (padrao)

Use baseline inicial por complexidade:

- `S`: 20 min
- `M`: 45 min
- `L`: 90 min

Se o time tiver historico proprio, substituir esta tabela.

## Formulas obrigatorias

Para cada teste:

- `ACTUAL_MINUTES = ceil((END_TIME_UTC - START_TIME_UTC) em minutos)`
- `SAVINGS_MINUTES = BASELINE_MANUAL_MINUTES - ACTUAL_MINUTES`
- `SAVINGS_PERCENT = (SAVINGS_MINUTES / BASELINE_MANUAL_MINUTES) * 100`
- `PRODUCTIVITY_RATIO = BASELINE_MANUAL_MINUTES / ACTUAL_MINUTES`

Consolidado do lote:

- `TOTAL_BASELINE_MINUTES = soma(BASELINE_MANUAL_MINUTES)`
- `TOTAL_ACTUAL_MINUTES = soma(ACTUAL_MINUTES)`
- `TOTAL_SAVINGS_MINUTES = TOTAL_BASELINE_MINUTES - TOTAL_ACTUAL_MINUTES`
- `TOTAL_SAVINGS_HOURS = TOTAL_SAVINGS_MINUTES / 60`
- `TOTAL_SAVINGS_PERCENT = (TOTAL_SAVINGS_MINUTES / TOTAL_BASELINE_MINUTES) * 100`

## Regra de qualidade da metrica

- so considerar economia de testes com `STATUS = PASS`
- testes `FAIL/BLOCKED` entram no relatorio de perdas/retrabalho

## Estrategia de classificacao

Classifique cada teste em:

- `NEW_TEST`
- `TEST_FIX`
- `REGRESSION_TEST`

Isso permite separar ganho em criacao vs manutencao.

## Saida obrigatoria

- log detalhado por teste (template markdown)
- consolidado por lote
- analise final com economia, gargalos e recomendacoes
