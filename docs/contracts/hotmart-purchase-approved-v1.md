# Contrato de compra aprobada de Hotmart V1

- **Estado:** Implementado localmente; DDL base verificado en Supabase;
  migración forward de seguridad, despliegue del bridge y E2E pendientes
- **Evento:** `PURCHASE_APPROVED`
- **Versión de payload:** `2.0.0`
- **Frontera autoritativa:** bridge + función transaccional de Postgres
- **Referencia externa:** [Hotmart Webhook — Request events](https://developers.hotmart.com/docs/en/2.0.0/webhook/purchase-webhook/)

## Propósito

Detener una recuperación de carrito cuando Hotmart informa una compra aprobada,
sin delegar al agente la correlación, el cierre del caso ni la decisión de
cancelar seguimientos.

## Admisión HTTP

`POST /webhooks/hotmart` aplica las mismas guardas que el abandono de carrito:

1. `X-HOTMART-HOTTOK` válido mediante comparación constante, antes de leer el
   cuerpo;
2. JSON válido dentro del límite fijo de 1 MiB, leído incrementalmente;
3. `id` externo no vacío;
4. `event = PURCHASE_APPROVED`;
5. `version = 2.0.0`;
6. `creation_date` dentro de la ventana anti-replay;
7. persistencia idempotente en `webhook_events` antes de responder `202`.

El receptor no correlaciona ni modifica el caso durante el request HTTP.

## Campos requeridos para procesamiento

```text
id
event
version
creation_date
data.product.id
data.buyer.email OR data.buyer.checkout_phone
data.purchase.status = APPROVED
data.purchase.transaction
data.purchase.approved_date
data.purchase.offer.code (opcional)
```

Normalización:

- email: `trim` + minúsculas;
- teléfono: sólo dígitos después de validar sintaxis convencional;
- producto: representación textual del identificador numérico;
- transacción: referencia Hotmart con formato `HP[A-Z0-9]{6,62}`.

Un payload incompleto o contradictorio termina como
`invalid_purchase_payload`; no invoca Hermes ni intenta correlación parcial.

El consumo asíncrono de `PURCHASE_APPROVED` por el resolution worker está
protegido por `HOTMART_PURCHASE_WORKER_ENABLED`, apagado por defecto. La
admisión HTTP sigue siendo durable con el flag apagado, pero el worker excluye
ese tipo de evento de su lote para que no bloquee abandonos. El flag sólo puede
activarse junto con `RESOLUTION_WORKER_ENABLED=true`.

Este flag no desactiva las guardas SQL fail-closed. Una compra durable conocida
puede igualmente impedir una recuperación creada después: una coincidencia
exacta se aplica por la guarda de orden inverso y una coincidencia ambigua pausa
el caso sin elegir contacto. La garantía de no contactar a un comprador tiene
precedencia sobre el apagado del consumidor asíncrono.

## Correlación autoritativa

La función `apply_hotmart_purchase_approved(...)` bloquea y verifica el evento
persistido antes de buscar candidatos.

La identidad candidata se obtiene por coincidencia exacta de email o teléfono
en `contacts` y `contact_points`. Luego se buscan casos que cumplan todo lo
siguiente:

- `source = hotmart`;
- mismo `external_product_id`;
- mismo `offer_code` con igualdad null-safe; una compra sin oferta sólo puede
  correlacionarse con un caso que también carezca de oferta;
- estado `grace_period`, `active` o `paused`;
- sin `purchase_event_id` previo;
- creados dentro de la ventana `expires_after` de su política y antes de la
  compra, con cinco minutos de tolerancia de reloj.

Sólo se aplica una compra cuando existe exactamente un contacto y exactamente
un caso candidato. El orden de locks es:

```text
webhook_event → identity tables → contact → recovery_case → followup_sequence
→ scheduled_action → delivery_attempt
```

## Resultado exacto

En una sola transacción:

1. el caso pasa a `won` y `lead_stage = won`;
2. se fija `purchase_event_id`, `won_at` y `closed_at`;
3. las secuencias activas o pausadas pasan a `completed` con
   `completion_reason = purchase_detected`;
4. las acciones `pending`, `deferred` o `retryable_failed` sin request iniciado
   pasan a `cancelled` con `terminal_reason = purchase_detected`;
5. un delivery attempt `reserved` se finaliza como `failed_before_request`;
6. un delivery attempt `request_started` se finaliza como `delivery_unknown`
   con deadline de reconciliación;
7. se registra `purchase_detected` en `conversation_events`;
8. el webhook pasa a `processed`.

Las acciones que ya están en `delivery_unknown` no se reclasifican como
canceladas porque puede existir un efecto externo previo. Si la compra compite
con un `followup_delivery_attempts.phase = request_started`, la acción pasa a
`delivery_unknown`, no a `cancelled`. El caso ganado impide nuevos envíos y la
finalización o reconciliación posterior conserva el outcome real; nunca se
interpreta como “no enviado”.

## Ambigüedad y ausencia

### Identidad o caso ambiguo

La función devuelve `ambiguous`, marca el webhook como `failed` y, si existen
casos candidatos, aplica fail-closed:

- pausa los casos activos;
- pausa sus secuencias;
- cancela acciones todavía no iniciadas;
- finaliza de forma coherente sus delivery attempts;
- registra `purchase_correlation_ambiguous`.

La resolución posterior es humana; no se elige el primer candidato.

### Contacto o caso no encontrado

La función devuelve `not_found` y marca el webhook como `failed` con un código
estable. No modifica casos de otro producto u oferta y requiere revisión
operativa.

## Idempotencia

- `(source, external_event_id)` evita admisiones duplicadas;
- un índice parcial único por `data.purchase.transaction` evita aplicar la
  misma transacción con IDs externos diferentes;
- `recovery_cases.purchase_event_id` es único;
- reejecutar una compra ya aplicada devuelve `already_applied`;
- una falla transitoria de la llamada RPC deja el evento `received` por rollback
  y permite reintento del worker sin bloquear el resto del batch;
- un rechazo RPC HTTP 4xx se cuarentena como
  `purchase_rpc_permanent_failure` para que no envenene la cola.

Si la compra llega antes que el abandono, una guarda diferida al planificar la
recuperación reevalúa compras conocidas. Sólo cierra cuando identidad,
producto, oferta y caso son únicos; en caso ambiguo no elige un candidato.

## Privacidad y logs

Los logs de aplicación sólo incluyen el ID externo del evento y el outcome. No
incluyen email, teléfono, payload completo ni transacción.

El payload completo permanece en `webhook_events` y en la captura privada según
las reglas de datos existentes.

## Límites de evidencia

Las pruebas locales verifican admisión HTTP, parsing, routing, contrato del RPC
y regresiones. Las migraciones fueron aceptadas por un parser PostgreSQL y se
ejecutaron sobre PGlite junto con el esquema completo. La prueba conductual
cubrió compra posterior al caso, compra anterior al abandono y request externo
ya iniciado preservado como `delivery_unknown`.

El DDL y sus permisos efectivos fueron verificados en Supabase. Esto no
constituye todavía evidencia de:

- bridge desplegado con esta versión;
- procesamiento de una compra real de Hotmart;
- cancelación observada end-to-end en producción.

Esas afirmaciones requieren aplicación controlada de la migración y una prueba
HTTP real con consulta posterior del caso, secuencia, acción y webhook.

La página oficial describe un payload común para varios eventos de compra y su
ejemplo no es evidencia de un delivery concreto de Lancemos. Por seguridad, V1
exige que `event = PURCHASE_APPROVED` y `purchase.status = APPROVED` coincidan;
una discrepancia se conserva como falla para revisión en vez de cerrar un caso.
