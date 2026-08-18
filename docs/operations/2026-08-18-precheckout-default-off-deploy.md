# Pre-checkout `purchase_intent`: DDL y despliegue default-off

- **Fecha:** 2026-08-18
- **Tipo:** evidencia operativa sanitizada
- **Commit productivo:** `62e601dc7a157d17fb7c2855d3e6a7bac7f39046`
- **PR de implementación:** #43
- **Alcance:** migración durable y nueva imagen del bridge, sin activar ingreso pre-checkout ni efectos outbound

## Estado implementado

La migración `20260814000200_precheckout_purchase_intents.sql` fue aplicada en Supabase Cloud desde el commit productivo. El bridge fue actualizado a una imagen inmutable identificada con el mismo commit.

Este despliegue no constituye activación del caso. El receiver provisional y todos los workers o efectos proactivos permanecieron apagados.

## Verificación previa

- suite Python completa: PASS;
- suite SQL/ACL canónica: PASS;
- revisión independiente residual: APPROVE;
- CI `verify` del PR #43: success;
- `db push --dry-run --include-all`: una única migración pendiente, `20260814000200_precheckout_purchase_intents.sql`.

## Evidencia de Supabase Cloud

Aplicación:

```text
20260814000200_precheckout_purchase_intents.sql: applied
post-apply dry-run: up to date; zero pending migrations
```

Postflight sanitizado:

```text
purchase_intents: present, 0 rows
precheckout_submissions: present, 0 rows
purchase_intent_submissions: present, 0 rows
precheckout_submission_conflicts: present, 0 rows
unsafe true flags: 0
```

La RPC `admit_precheckout_form_submission(text,jsonb,jsonb)` quedó:

```text
SECURITY DEFINER: true
search_path: pg_catalog, public, pg_temp
PUBLIC EXECUTE: false
anon EXECUTE: false
authenticated EXECUTE: false
service_role EXECUTE: true
```

## Evidencia del bridge

Imagen efectiva:

```text
easypanel/infra/appointment-bridge:62e601dc7a157d17fb7c2855d3e6a7bac7f39046
```

Probe HTTP real dentro del task productivo:

```text
/health: HTTP 200, status=ok
/ready: HTTP 200, status=ready, automation_state=paused
POST /webhooks/precheckout: HTTP 503 mientras el receiver está apagado
```

Flags sanitizados posteriores al deploy:

```text
PRECHECKOUT_FORM_ENABLED=false
PRECHECKOUT_TEST_MODE_ENABLED=false
DURABLE_OUTBOUND_ENABLED=false
DURABLE_DISPATCHER_ENABLED=false
RESOLUTION_WORKER_ENABLED=false
HOTMART_PURCHASE_WORKER_ENABLED=false
CHATWOOT_CUT_B_ADMISSION_ENABLED=true
```

El inbound regular existente permaneció habilitado. No se publicó policy o scope, no se creó cohorte, no se admitió submission y no se envió ningún mensaje proactivo.

## Próximo gate separado

La activación test-only requiere otra decisión operativa y debe conservar:

- un único JID allowlisted;
- token pre-checkout separado;
- coincidencia exacta entre JID canónico, teléfono de prueba y submission;
- cero outbound en el primer probe de ingreso;
- revalidación de backlog y flags inmediatamente antes de armar cualquier sender.

Un redeploy posterior desde el control plane debe preservar explícitamente los dos flags pre-checkout en `false` hasta ese gate.
