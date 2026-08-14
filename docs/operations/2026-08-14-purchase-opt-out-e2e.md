# Evidencia — cierre E2E por compra Hotmart

- **Fecha:** 2026-08-14
- **Estado:** `verificada`
- **Entorno:** Supabase Cloud y bridge productivo, scope sintético descartable
- **Scope:** `e2e-purchase-20260814t151842z`, versión `2`
- **PII/secretos:** omitidos; la evidencia usa sólo estados, conteos y reason codes

## Alcance autorizado

Se probó un único contacto WhatsApp allowlisted con:

1. abandono de carrito Hotmart sintético;
2. conversación inbound real por WABA;
3. `PURCHASE_APPROVED` sintético correlacionado;
4. cierre durable por compra;
5. redelivery semánticamente idéntica.

Durante toda la prueba `DURABLE_DISPATCHER_ENABLED=false` y
`DURABLE_OUTBOUND_ENABLED=false`. No se habilitó envío automático de follow-ups.

## Precondiciones y reconciliaciones

La primera planificación falló cerrada por
`channel_identity_inbox_mismatch`: el contacto existía en Chatwoot inboxes `1` y
`7`, mientras la identidad durable todavía apuntaba a la conversación legacy de
inbox `1`. Con autorización explícita y backup privado se reconciliaron juntos
`metadata.inbox_id=7` y `external_conversation_id` hacia la única conversación
WABA abierta de inbox `7`.

La primera ejecución de compra quedó retryable en `received` porque
`apply_hotmart_purchase_approved`, que era `SECURITY INVOKER`, no tenía `UPDATE`
sobre `public.followup_delivery_attempts`. PostgreSQL exige ese privilegio para
`SELECT ... FOR UPDATE`, aunque el conjunto coincidente esté vacío. Se aplicó el
hotfix temporal autorizado:

```sql
grant update on table public.followup_delivery_attempts to service_role;
```

La verificación posterior devolvió `update_granted=true`. Este grant amplio
resolvió la corrida, pero reabrió DML directo y no es la remediación durable.

## Evidencia observada

### Abandono y conversación

```text
abandonment_event_status=processed
recovery_case_status=grace_period
identity_resolution_status=resolved
followup_sequence_status=active
scheduled_action_status=pending
delivery_attempts=0
outbound_authorizations=0
inbound_public_messages=1
agent_public_replies=1
```

El agente respondió al inbound real sin inventar detalles de la oferta sintética.

### Cierre por compra

```text
purchase_event_status=processed
purchase_processing_error=NULL
recovery_case_status=won
purchase_event_linked=true
won_at_set=true
closed_at_set=true
next_contact_at_cleared=true
next_contact_reason=purchase_detected
followup_sequence_status=completed
completion_reason=purchase_detected
scheduled_action_status=cancelled
terminal_reason=purchase_detected
successor_actions=0
delivery_attempts=0
outbound_authorizations=0
```

### Replay e idempotencia

El replay HTTP exacto posterior a la ventana de cinco minutos fue rechazado
fail-closed con HTTP `401` y `stale_webhook`. Para probar idempotencia durable se
envió una redelivery fresca con nuevo ID de entrega y la misma tupla semántica de
compra. El receptor devolvió HTTP `200`, `status=duplicate`.

Postconditions posteriores:

```text
case_status=won
case_version_unchanged=true
purchase_event_rows=1
semantic_conflicts=0
action_rows=1
action_still_cancelled=true
purchase_detected_projections=1
delivery_attempts=0
outbound_authorizations=0
```

## Cleanup verificado

```text
pilot_runtime_state=paused
pilot_runtime_generation=9
RESOLUTION_WORKER_ENABLED=false
HOTMART_PURCHASE_WORKER_ENABLED=false
DURABLE_DISPATCHER_ENABLED=false
DURABLE_OUTBOUND_ENABLED=false
ready_http=200
ready_reason=pilot_runtime_paused
```

El caso sintético y sus eventos se conservaron como evidencia; no se eliminaron
ni reabrieron.

## Fix reproducible pendiente de integración

El hotfix remoto sigue aplicado hasta desplegar la remediación. La migración local
`20260814000100_hotmart_purchase_worker_table_acl.sql` convierte el entrypoint en
`SECURITY DEFINER`, revoca `INSERT`, `UPDATE` y `DELETE` directos a `service_role`
y conserva solamente `EXECUTE` sobre la RPC. Así el lock requerido corre con la
autoridad de la función sin permitir reescrituras directas desde PostgREST.

Los probes PGlite comprueban dos propiedades conjuntamente:

- `service_role` ejecuta `apply_hotmart_purchase_approved` y completa el cierre;
- `service_role` no puede ejecutar `UPDATE` directo sobre la tabla de intentos.

Evidencia focal local:

```text
acl_hardening=OK
positive_control_leaks=6
public_functions=66
service_entrypoints=27
```

La migración local no se declara integrada ni desplegada hasta completar el flujo
de review correspondiente.
