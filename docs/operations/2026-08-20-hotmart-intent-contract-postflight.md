# Postflight contract de correlación Hotmart ↔ purchase intent

- **Fecha UTC:** 2026-08-20T19:36:24Z
- **Tipo:** evidencia operativa sanitizada
- **Estado:** aplicado y verificado en Appointment Bridge y Supabase Cloud
- **PR:** #54
- **Merge:** `3c4140d59051a0bfb568c639ac9c7fd84bf42b01`
- **Migración:** `20260820000400_hotmart_intent_correlation_contract.sql`
- **SHA-256 de migración:** `8a22982a451e18917037f0ab954960787681c49303a6d7bfcfcfcb273222eec1`

No contiene Hottok, service keys, firmas, payloads completos, emails ni teléfonos.

## Objetivo

Cerrar la fase contract después del rollout expand:

1. desplegar un bridge sin métodos cliente legacy;
2. demostrar un único task sano sobre la imagen contract;
3. revocar los dos shims históricos para `service_role`;
4. preservar los wrappers correlacionados;
5. demostrar rechazo legacy y delta comercial cero.

No habilita contacto, workers, dispatcher, follow-ups, WhatsApp ni email.

## Gates previos al merge

- revisión independiente final: `APPROVE`;
- pytest: **952 PASS**;
- PostgreSQL 17: **29 migraciones** y tres probes PASS;
- PGlite/npm: **89 funciones públicas**, **33 entrypoints**, cero leaks o mismatches;
- pglast, compileall, lock, build, diff check y preflight: PASS;
- CI requerido `verify` del PR #54: PASS.

## Gate de réplicas

Antes de preparar el contract se verificó:

- un único servicio `infra_appointment-bridge`;
- un único task activo;
- imagen correlacionada inmutable previa `7a90269…`;
- contenedores históricos `latest` detenidos;
- cero callers legacy en `src/`;
- workers y outbound apagados.

## Despliegue contract

El bridge se construyó en la VPS desde un `git archive` del merge exacto.

```text
archive_sha256=865e1e8b1390461c6226d18e6a7ac77f997625f977f944eb1a034a4b847e37db
image_tag=easypanel/infra/appointment-bridge:3c4140d59051a0bfb568c639ac9c7fd84bf42b01
image_id=sha256:486a21f8c1b0b6ce6f9330d5857a49386cd5de102c52d10db310de9beb73e772
running_tasks=1
```

Dentro de un contenedor recreado desde esa imagen se verificó, usando el intérprete ya
instalado y sin sincronizar paquetes:

```text
SupabaseClient.admit_hotmart_purchase_approved=absent
SupabaseClient.admit_hotmart_cart_abandonment=absent
```

Estado HTTP después del deploy:

```text
GET /health=200
GET /ready=200
POST /webhooks/hotmart sin Hottok=401
```

Flags explícitos observados:

```text
HOTMART_PURCHASE_WORKER_ENABLED=false
RESOLUTION_WORKER_ENABLED=false
DURABLE_DISPATCHER_ENABLED=false
DURABLE_OUTBOUND_ENABLED=false
```

`PRECHECKOUT_FIRST_TOUCH_ENABLED` estaba ausente y conserva su default seguro `false`.

## Aplicación Cloud

El helper de release exigió simultáneamente:

- worktree detached limpio en el merge exacto;
- filename y SHA-256 exactos;
- dry-run con un único pendiente `20260820000400`.

Resultados:

```text
dry-run: success=true, pending=[20260820000400]
apply: success=true, reported_versions=[20260820000400]
Cloud migration history: 20260820000400 trackeada
```

## ACL efectiva

Postflight sobre firmas exactas:

| Firma | PUBLIC | anon | authenticated | service_role |
|---|---:|---:|---:|---:|
| `admit_hotmart_purchase_approved(text,jsonb)` | false | false | false | **false** |
| `admit_hotmart_cart_abandonment(text,jsonb)` | false | false | false | **false** |
| `admit_and_correlate_hotmart_purchase_approved(text,jsonb,text,text)` | false | false | false | **true** |
| `admit_and_correlate_hotmart_cart_abandonment(text,jsonb,text,text)` | false | false | false | **true** |

Las cuatro funciones conservan owner `postgres`, `SECURITY DEFINER` y:

```text
search_path=pg_catalog, public, pg_temp
```

## Probe HTTP server-side

Desde el contenedor productivo se usó la credencial server-side sin imprimirla. Se
mandaron sólo payloads inválidos sin PII para probar frontera y rollback:

```text
legacy purchase RPC=403
legacy cart RPC=403
correlated purchase wrapper=400, alcanzó validación
correlated cart wrapper=400, alcanzó validación
```

La consulta posterior confirmó:

```text
external_event_id=contract-denied-probe → 0 filas
external_event_id=contract-wrapper-negative-probe → 0 filas
```

El 400 de wrappers no acredita un evento Hotmart válido nuevo; demuestra que el rol aún
alcanza la función canónica y que el payload inválido falla antes de persistir. El E2E
válido correlacionado previo está documentado por separado.

## Delta de efectos

Antes del deploy/migración:

```text
correlations=5
candidates=5
scheduled_actions=9
followup_delivery_attempts=9
```

Después del postflight:

```text
correlations=5
candidates=5
scheduled_actions=9
followup_delivery_attempts=9
```

Resultado: delta cero en correlaciones, candidatos, acciones e intentos. Los valores
`9/9` son registros preexistentes; no son efectos de este release.

## Límite de la evidencia

Este postflight demuestra el cierre técnico de la fase contract y la continuidad de las
fronteras canónicas. No demuestra una entrega originada oficialmente por Hotmart ni una
compra real del cliente. Esa procedencia sigue pendiente y debe registrarse aparte.
