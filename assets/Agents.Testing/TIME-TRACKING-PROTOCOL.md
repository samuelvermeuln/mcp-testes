# Time Tracking Protocol (por teste)

## Objetivo

Padronizar a medicao de tempo de escrita de cada teste para comparacao com baseline manual.

## Regra central

Toda criacao ou ajuste de teste deve ter:

- `START_TIME_UTC`
- `END_TIME_UTC`
- `ACTUAL_MINUTES`

Sem isso, o teste nao entra no calculo de economia.

## Momento de inicio e fim

- Inicio: imediatamente antes de escrever/alterar o primeiro bloco do teste.
- Fim: quando o teste estiver verde no comando de validacao do escopo.

## Exemplo rapido em Bash

```bash
# inicio
START_TIME_UTC=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# ... escrever/ajustar teste ...
# ... rodar dotnet test do escopo ...

# fim
END_TIME_UTC=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

echo "START_TIME_UTC=$START_TIME_UTC"
echo "END_TIME_UTC=$END_TIME_UTC"
```

## Exemplo rapido em PowerShell

```powershell
# inicio
$StartTimeUtc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")

# ... escrever/ajustar teste ...
# ... rodar dotnet test do escopo ...

# fim
$EndTimeUtc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")

Write-Host "START_TIME_UTC=$StartTimeUtc"
Write-Host "END_TIME_UTC=$EndTimeUtc"
```

## Calculo de minutos (regra)

```text
ACTUAL_MINUTES = ceil((END_TIME_UTC - START_TIME_UTC) em minutos)
```

Use arredondamento para cima para manter consistencia entre operadores.

## Casos especiais

- Se houver bloqueio externo (ambiente, credencial, dependencia):
  - marcar `STATUS=BLOCKED`
  - registrar minutos ja consumidos
- Se houver retrabalho apos falha:
  - manter mesmo `TEST_CASE_ID`
  - adicionar nota de retrabalho

## Registro oficial

Ao final de cada teste, atualizar imediatamente:

- `TEST-METRICS-LOG-TEMPLATE.md` (instancia de execucao)
- `AI-SAVINGS-REPORT-TEMPLATE.md` (consolidado da rodada)
