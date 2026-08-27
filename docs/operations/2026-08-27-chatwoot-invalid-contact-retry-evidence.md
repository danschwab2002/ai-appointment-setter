# Evidencia controlada: retry acotado de contacto Chatwoot

- **Fecha:** 2026-08-27
- **Estado:** implementación local verificada; migración no aplicada; código no publicado ni desplegado
- **Alcance:** respuesta oficial `payload.contact.id` y un retry durable de `invalid_contact_id`

## Incidente observado

Un `PURCHASE_CANCELED` autenticado, correlacionado y autorizado alcanzó la creación
de contacto en Chatwoot. Chatwoot respondió `2xx` con el ID bajo
`payload.contact.id`, forma que el adaptador no reconocía. El bridge finalizó el
command como:

```text
status = delivery_unknown
failure_code = invalid_contact_id
chatwoot_conversation_id = null
chatwoot_message_id = null
```

No se observó creación de conversación ni mensaje. Esta evidencia no autoriza un
retry genérico de `delivery_unknown`.

## Implementación verificada

- `ChatwootClient.create_contact` acepta `payload.contact.id` y conserva las formas
  históricas.
- El command incorpora `invalid_contact_retry_count`, restringido a `0..1`.
- La RPC service-role-only
  `prepare_johanna_payment_failure_invalid_contact_retry` sólo arma el retry cuando
  el predecessor coincide exactamente con `invalid_contact_id`, no tiene IDs
  remotos y el contador es cero.
- Antes del nuevo `request_started`, la RPC relee scope/runtime, intención,
  consentimiento, opt-out, ownership/contact permission y presupuesto compartido.
- El sender del retry usa `require_existing_contact=true`: una búsqueda exacta sin
  contacto retorna `existing_contact_required`; no crea otro contacto.
- Otros `delivery_unknown`, un segundo prepare y un stop nuevo no producen sender.

## Evidencia ejecutable

TDD observó RED antes de los cambios focales:

- `payload.contact.id` terminaba en `invalid_contact_id`;
- el router devolvía el replay terminal sin consultar una autorización de retry;
- el sender no aceptaba el fence `require_existing_contact`;
- la migración incremental no existía.

Verificación final:

```text
uv run pytest
1152 passed, 1 warning deprecatorio de Starlette

cd tests/sql/followup_engine && npm test
PASS
JOHANNA_PAYMENT_FAILURE_DURABLE_REVIEW_OK
acl_hardening=OK; service_entrypoints=48
```

El validador SQL físico comprobó:

```text
response_lost -> not_retryable, sin mutación
invalid_contact_id -> retry_started, contador 1
segundo prepare -> not_retryable, contador 1
opt-out previo -> rechazo fail-closed, predecessor preservado
anon/authenticated execute -> false
service_role execute -> true
```

Un probe HTTP real por loopback levantó el ASGI de la rama y envió un duplicate
`delivery_unknown` elegible usando autoridad y sender stateful sin red externa:

```text
HTTP: 202
prepare calls: 1
sender calls: 1
finish calls: 1
require_existing_contact: true
external calls: 0
server stopped: true
```

Este probe demuestra TCP local, lifespan, autenticación/parsing, orquestación y el
fence del sender. No demuestra aplicación de la migración en Supabase Cloud,
deploy productivo, aceptación real de Chatwoot ni entrega física por WhatsApp.

## Artefacto de migración

```text
supabase/migrations/20260827000200_chatwoot_invalid_contact_retry.sql
```
