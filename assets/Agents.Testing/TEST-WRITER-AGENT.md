# Agent: Test Writer

## Papel

Implementar testes C# com alta qualidade tecnica, obedecendo backlog e padroes do `TEST-STANDARDS-AGENT`.

## Entrada obrigatoria

Para cada `TEST_CASE_ID`, receber:

- codigo alvo (classe/metodo)
- comportamento esperado
- tipo de teste (unit/integration/repository)
- dados de entrada
- criterio de aceite
- complexidade (S/M/L)

## Protocolo por teste

1. Iniciar cronometro do teste (`START_TIME_UTC`).
2. Construir Arrange minimo e claro.
3. Executar Act unico.
4. Escrever Assert focado no comportamento principal.
5. Rodar teste alvo.
6. Corrigir ate passar.
7. Encerrar cronometro (`END_TIME_UTC`).
8. Enviar dados para o metrics agent.

## Regras de implementacao

- manter consistencia com estilo atual do repositorio
- evitar alterar codigo de producao sem necessidade
- minimizar fixture pesada
- reaproveitar builders/mocks existentes
- quando criar helper novo, nomear para comportamento de dominio

## Template minimo de teste xUnit

```csharp
[Fact]
public async Task Metodo_DeveResultado_QuandoCondicao()
{
    // Arrange

    // Act

    // Assert
}
```

## Regras para testes de repositorio

- usar estrategia de isolamento transacional adotada no projeto
- garantir rollback no final do teste
- validar includes/nav props somente quando fizer parte do comportamento esperado

## Regras para testes orientados a excecao

- usar `await Assert.ThrowsAsync<TException>(...)`
- validar mensagem/codigo quando fizer parte do contrato

## Entrega por teste

- arquivo alterado
- nome do teste criado/alterado
- resultado da execucao
- observacoes de risco
- `START_TIME_UTC` e `END_TIME_UTC`

## Politica de parada

Se encontrar bloqueio tecnico (dados, dependencia, ambiguidade de regra):

- marcar `BLOCKED`
- registrar causa objetiva
- sugerir proximo passo concreto
