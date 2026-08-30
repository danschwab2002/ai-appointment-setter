# Evidencia local de preparación productiva del first-touch pre-checkout

- **Fecha:** 2026-08-30 UTC
- **Estado:** verificación local completa; no desplegado ni activado
- **Branch:** `feat/precheckout-production-readiness`
- **Base verificada:** `30e843d07246025712ca5620cc602d2ac85345a6`
- **Outbound real:** no ejecutado

## Alcance demostrado

La migración preparatoria publica un scope dedicado con presupuesto `1/1/1`,
runtime `inactive/generation=0`, política de timer de 60 minutos y binding con
`precheckout_first_touch_enabled=false`. La RPC sanitaria queda limitada a
`service_role`. Cuando el flag de proceso está encendido, `/ready` exige tracking,
scope, runtime, policy/delay y binding exactos y falla cerrado ante contradicciones
o reason codes desconocidos. Un probe acumulativo demuestra que el binding
histórico de 5 minutos se migra a la policy dedicada de 60 minutos, incrementa su
generación y permanece apagado.

La preparación no arma runtime, no crea timers ni comandos y no llama Chatwoot,
WABA, Meta ni Hermes.

## Puertas ejecutadas

```text
uv run pytest
1223 passed, 1 warning preexistente

npm test
PASS, incluyendo PRECHECKOUT_PRODUCTION_READINESS_SQL_OK,
PRECHECKOUT_PRODUCTION_READINESS_LEGACY_BINDING_OK y ACL hardening

DATABASE_URL=<PostgreSQL 17 descartable> \
ALLOW_DISPOSABLE_DATABASE=followup-concurrency \
npm run test:real-postgres
real_postgres_migration_apply=OK
real_postgres_two_active_sessions=OK
real_postgres_lock_wait=OK
serialized_concurrent_acceptance_replay=OK
canonical_rows_and_successor=OK

git diff --check
PASS
```

PostgreSQL utilizado: 17.10, clúster local descartable, TCP localhost. Después del
probe se detuvo el proceso, `pg_isready` confirmó `no response` y se eliminaron el
clúster y su archivo de ubicación.

## Límites de la evidencia

Esta corrida no demuestra:

- aplicación de `20260829000500` en Supabase Cloud;
- reconciliación remota del tracking `00200`–`00500`;
- revisión exacta desplegada por EasyPanel;
- valor efectivo del flag en EasyPanel;
- aprobación/sincronización Meta del template
  `johanna_interes_precheckout_01`;
- activación durable;
- aceptación, entrega o lectura WABA.

Mientras esos gates no estén demostrados, el first-touch debe permanecer apagado.
Un timeout, `502` o resultado ambiguo nunca autoriza retry del POST externo.
