# Evidencia local — receiver observado `lead.precheckout` v1

- **Fecha:** 2026-08-19
- **Estado:** evidencia local; no desplegado ni conectado a la landing
- **Rama:** `feat/lead-precheckout-v1-receiver`
- **Alcance:** recepción autenticada, admisión durable, idempotencia, identidad y ACL

## Entorno

- Python 3.13 administrado con `uv`.
- PostgreSQL 17.10 descartable, extraído localmente desde paquetes Debian.
- Sin Docker, credenciales externas, datos productivos ni acceso de escritura a Supabase Cloud.
- Receiver, workers, dispatcher y outbound productivos permanecieron apagados.

## Aplicación de esquema

El probe versionado aplicó desde cero:

1. `supabase/baseline/20260803_public_schema.sql`;
2. todas las migraciones de `supabase/migrations/` en orden;
3. defaults de roles equivalentes a la superficie API relevante.

Resultado:

```text
observed_lead_real_postgres_migrations=OK
```

## Comportamiento verificado

Comando:

```text
uv run python tests/sql/followup_engine/real_postgres_observed_lead.py
```

Marcadores obtenidos:

```text
observed_lead_insert_and_replay=OK
observed_lead_semantic_conflict_replay=OK
observed_lead_double_submit_one_intent=OK
observed_lead_same_phone_changed_email_fail_closed=OK
observed_lead_email_only_phone_backfill=OK
observed_lead_known_phone_not_degraded=OK
observed_lead_nullable_phone_fail_closed=OK
observed_lead_exact_replay_concurrency=OK
observed_lead_distinct_submit_concurrency=OK
observed_lead_crossed_identity_concurrency=OK
observed_lead_acl=OK
observed_lead_late_failure_rollback=OK
OBSERVED_LEAD_PRECHECKOUT_REAL_POSTGRES_OK
```

Esto prueba en PostgreSQL real:

- insert durable y replay exacto;
- conflicto semántico durable y replay sin duplicarlo;
- submissions distintos de la misma identidad unidos a un solo `purchase_intent`;
- correlación por email o teléfono;
- mismo teléfono con email diferente clasificado `identity_conflict`, sin autorización de contacto;
- teléfono válido posterior incorporado a un intent email-only sin crear otro intent;
- teléfono conocido preservado ante una entrega posterior sin teléfono;
- teléfono inválido/ausente persistido como `NULL`, `tracking_incomplete` y no contactable;
- serialización concurrente para retry exacto, doble submit y correlaciones cruzadas sin deadlock;
- `EXECUTE` sólo para `service_role` y ausencia de DML directo;
- rollback atómico cuando falla una sentencia posterior.

## Hallazgo durante la prueba

La primera ejecución descubrió que un teléfono ya observado con otro email podía chocar con `purchase_intents_one_live_identity_idx` y producir un error en vez de una correlación fail-closed.

Se corrigió la RPC para:

- tomar advisory locks separados y ordenados por email y teléfono;
- buscar intents activos por ambos identificadores y tomar sus row locks por UUID ascendente;
- reutilizar un único intent;
- enriquecer de forma monotónica un teléfono faltante cuando el match por email es inequívoco;
- marcar `identity_conflict` ante evidencia cruzada;
- mantener `whatsapp_contact_authorized=false`.

La prueba completa se repitió desde una base vacía después de la corrección y pasó.

## CI

`.github/workflows/ci.yml` ejecuta el mismo probe contra el servicio efímero `postgres:17` en cada PR y push a `main`.

## Límites

Esta evidencia no prueba:

- migración aplicada en Supabase Cloud;
- tracking remoto de migraciones;
- deploy del bridge;
- conexión de la landing;
- autorización de WhatsApp;
- activación de workers o outbound;
- E2E productivo.

Esas mutaciones conservan gates y autorizaciones separadas.
