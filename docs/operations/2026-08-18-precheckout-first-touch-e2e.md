# First touch pre-checkout test-only: DDL, deploy y E2E

- **Fecha:** 2026-08-18
- **Tipo:** evidencia operativa sanitizada
- **Merge commit:** `720a70344ef464964328db521caf56967ba6abb5`
- **Commit revisado:** `35104e05b7339826e62a934ea5b2c33309e8252b`
- **PR:** #46
- **Alcance:** un único mensaje WABA manual al único JID allowlisted; cero follow-ups

## Límites

Este corte no clasifica abandono ni fallo de pago, no acredita consentimiento
comercial general y no habilita scheduler, dispatcher, workers Hotmart ni outbound
durable general. La autorización fue test-only, one-shot y no generalizable.

## Verificación previa

- suite Python completa: PASS;
- suite SQL/PGlite completa: PASS;
- compileall y `git diff --check`: PASS;
- revisión independiente inicial: REQUEST_CHANGES;
- tres bloqueos corregidos: locks canónicos, preservación de `delivery_unknown` y
  presupuesto singleton del rollout;
- revisión residual independiente: APPROVE;
- CI `verify` del PR #46: success.

## Supabase Cloud

El dry-run listó exactamente:

```text
20260818000100_precheckout_test_first_touch.sql
```

La migración se aplicó y quedó registrada una sola vez. Postflight sanitizado:

```text
tabla durable: presente
RPC begin y finish: presentes
command rows antes del E2E: 0
scope singleton: presente
service_role EXECUTE RPCs: true
service_role SELECT tabla: false
anon/authenticated EXECUTE begin: false
```

Los advisors no reportaron un problema de seguridad nuevo asociado al corte. Los
avisos nuevos de la tabla fueron índices de foreign keys de nivel informativo; no
bloquean un singleton de una fila.

## Bridge

Imagen desplegada:

```text
easypanel/infra/appointment-bridge:720a70344ef464964328db521caf56967ba6abb5
```

Después del reemplazo:

```text
/health: HTTP 200
/ready: HTTP 200
```

El receiver pre-checkout test-only permaneció activo para el teléfono exacto. Los
consumidores generales permanecieron apagados:

```text
DURABLE_OUTBOUND_ENABLED=false
DURABLE_DISPATCHER_ENABLED=false
RESOLUTION_WORKER_ENABLED=false
HOTMART_PURCHASE_WORKER_ENABLED=false
```

## Barrera inmediatamente anterior

```text
intenciones elegibles: 1
bindings canónicos elegibles: 1
first-touch commands: 0
scheduled actions elegibles: 0
attempts request_started: 0
template libre_ansiedad_test_first_touch_v1: approved
```

La identidad fue validada contra account, inbox y conversación canónica. No se
leyeron ni registraron nombre, teléfono, contenido de conversación ni payloads.

## E2E real

Se habilitó temporalmente `PRECHECKOUT_FIRST_TOUCH_ENABLED` con un token efímero,
se disparó el endpoint interno una vez y un trap operativo volvió a apagar el gate
y retiró el token.

Respuesta del bridge:

```text
HTTP 202
status=accepted_by_chatwoot
message_count=1
followups_allowed=0
```

Traza durable posterior:

```text
command_count=1
accepted_by_chatwoot=1
request_started=0
delivery_unknown=0
IDs Chatwoot positivos=true
test_only=true
generalizable=false
max_messages=1
followups_allowed=0
scheduled actions elegibles=0
```

Chatwoot confirmó un mensaje `outgoing`, `text`, con `source_id` externo presente.
El estado observado durante dos minutos permaneció `sent`; no alcanzó `delivered`
ni `read` dentro de esa ventana. Por lo tanto, esta evidencia acredita aceptación
por Chatwoot/Meta y creación del mensaje externo, pero **no acredita entrega al
dispositivo**.

## Estado final seguro

```text
PRECHECKOUT_FIRST_TOUCH_ENABLED=false
PRECHECKOUT_FIRST_TOUCH_TOKEN ausente
/health: HTTP 200
/ready: HTTP 200
```

El presupuesto singleton ya fue consumido. Una intención sucesora o una command
distinta no puede producir un segundo envío en este rollout V1.
