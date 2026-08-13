# Deadlines absolutos para seguimientos

- **Estado:** Implementada localmente; no aplicada en Supabase Cloud
- **Alcance:** semántica temporal de `followup_policy_versions.steps[].delay`
- **No autoriza:** publicar policies, scope, workers, dispatcher, outbound o mensajes

## Problema observado

El finalizador vigente materializaba cada sucesor con `p_now + delay`. Una policy
`2/5/10` producía aproximadamente `+2/+7/+17` porque cada delay comenzaba luego
de aceptar el mensaje anterior.

## Semántica propuesta e implementada localmente

Cada `delay` es un offset absoluto desde la primera aceptación outbound durable
de la secuencia:

```text
T = min(accepted_at) de intentos accepted_by_chatwoot de la secuencia
due_at(step) = T + steps[step].delay
```

La primera aceptación es un ancla durable y observable. `started_at` no sirve
como ancla porque puede preceder al primer efecto por colas o ventanas de negocio.
Si una aceptación intermedia ocurre tarde, el siguiente deadline no se desplaza:
queda vencido y sólo podrá avanzar tras la re-evaluación autoritativa normal.

## Evidencia local completada

- PGlite verifica el caso tardío y la cadena `0/+2/+5/+10`;
- PostgreSQL 17 aplica baseline y 18 migraciones, verifica fingerprints y ACL;
- la suite Python completa y el bundle determinista pasan localmente.

## Gates operacionales pendientes

- revisión independiente;
- autorización separada para DDL remoto;
- postflight remoto con runtime todavía apagado.