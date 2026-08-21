# ADR-0014: Timer configurable de reevaluación de abandono

- **Estado:** Aceptada
- **Fecha:** 2026-08-21
- **Estado de implementación:** Implementada localmente; no desplegada ni activada
- **Complementa:** [ADR-0007](0007-durable-next-action-engine.md)

## Contexto

La correlación determinística de Hotmart ya puede resolver
`PURCHASE_OUT_OF_SHOPPING_CART` contra una `purchase_intent` y clasificarla como
`confirmed_abandonment`. Esa clasificación no programa hoy una espera durable.

El plazo no puede ser una constante global. Cada infoproductor debe poder elegir
un plazo distinto y, cuando haga falta, definir un override más específico para
un producto u oferta. El valor que originó un timer debe quedar congelado para
que una configuración posterior no cambie timers existentes silenciosamente.

El motor durable de ADR-0007 ya define políticas publicadas, inmutables y
versionadas mediante `followup_policy_versions`. Su `grace_period` expresa la
espera anterior al primer contacto. Sin embargo, `recovery_cases` y
`scheduled_actions` requieren un `contact_id` canónico. El flujo observado de
`purchase_intents` todavía no crea ni vincula un contacto y no debe inventarlo a
partir de email o teléfono durante el webhook.

## Decisión

### 1. Reutilizar la política temporal existente

El timer toma su duración de `followup_policy_versions.grace_period`. No se crea
una segunda definición de tiempos.

Una asignación activa selecciona una política publicada para el scope:

```text
tenant_ref + funnel_ref                     → default del infoproductor
+ product_ref opcional                      → override de producto
+ offer_ref opcional, sólo con product_ref  → override de oferta
```

En este producto, `funnel_ref` identifica al infoproductor dentro del tenant
administrado. La selección elige el scope más específico. Un override específico
deshabilitado prevalece sobre un default más general habilitado.

No existe asignación implícita. La ausencia de configuración o una asignación
deshabilitada significa **no programar**.

### 2. Validar el número antes de activarlo

La demora efectiva debe:

- ser mayor o igual a 60 segundos;
- ser menor o igual a 30 días;
- tener precisión de segundos enteros;
- pertenecer a una política `published` con `purpose=cart_recovery`;
- ser menor que `expires_after` de esa misma política.

Una asignación inválida falla cerrada y no puede activarse.

### 3. Congelar la configuración en cada timer

Al programar se persisten:

- asignación y generación seleccionadas;
- `policy_key` y `policy_version`;
- `delay_seconds_snapshot`;
- evento Hotmart de origen;
- `observed_at` correlacionado;
- `due_at = observed_at + delay_seconds_snapshot`;
- clave de idempotencia estable.

Cambiar la asignación o publicar una política nueva afecta sólo timers creados
después del cambio. Reprogramar timers existentes requiere una operación futura
explícita y auditable.

### 4. Programar sólo desde evidencia resuelta

Sólo se programa cuando la misma transacción confirma:

```text
correlation.outcome=resolved
correlation.event_type=PURCHASE_OUT_OF_SHOPPING_CART
purchase_intent.lifecycle_state=waiting_for_purchase
purchase_intent.current_classification=confirmed_abandonment
```

`conflict`, `ambiguous`, `unmatched`, eventos inválidos, scopes sin política y
políticas deshabilitadas crean cero timers.

El replay del mismo `webhook_event_id` reutiliza exactamente el mismo timer. Un
evento de abandono distinto también reutiliza el timer mientras exista uno
`scheduled` para la intención; en ambos casos no mueve `due_at` ni cambia el
snapshot. Después de completar ese timer, un evento de abandono nuevo y
autoritativo puede iniciar un ciclo nuevo con la configuración vigente.

### 5. Mantener el timer antes del scheduler comercial

La reevaluación vinculada a `purchase_intent` es una cola pre-comercial. No crea
`recovery_case`, `followup_sequence`, `scheduled_action`, intento de entrega ni
solicitud outbound.

Esta separación evita fabricar un `contact_id`. La futura promoción al scheduler
de ADR-0007 deberá requerir un binding canónico y revalidar autorización,
opt-out, takeover, scope y demás guardas en una transición atómica separada.

### 6. Releer el estado al vencer

El worker consulta IDs vencidos y llama una RPC idempotente por timer. La RPC
bloquea primero la `purchase_intent` y después el timer, y decide:

```text
purchased
→ completed / cancelled_purchased

sigue confirmed_abandonment pero falta autorización
→ completed / blocked_not_authorized

sigue confirmed_abandonment y las banderas locales están autorizadas,
pero no existe binding canónico a contact
→ completed / blocked_contact_binding_missing

cualquier otro estado
→ completed / cancelled_intent_changed
```

No existe resultado `execute` ni integración outbound en esta fase.

### 7. Procesar at-least-once sin lease externo

Listar vencidos no concede autoridad. Dos workers pueden observar el mismo ID,
pero la RPC serializa por intención, relee el timer y devuelve el resultado ya
persistido en replays.

Esta cola no necesita reservar un efecto externo porque todos sus resultados son
internos y terminales. Si en el futuro la misma RPC promueve hacia una acción
comercial, esa promoción deberá ser atómica e idempotente o incorporar fencing
antes de habilitar múltiples workers.

### 8. Mantener activación default-off

El runtime incorpora un worker específico, deshabilitado por defecto. Activarlo
sólo habilita reevaluaciones internas; no activa `RESOLUTION_WORKER_ENABLED`,
`DURABLE_DISPATCHER_ENABLED` ni `DURABLE_OUTBOUND_ENABLED`.

No se publica una asignación para Johanna sin que el operador configure el plazo
aprobado por el infoproductor.

## Consecuencias

### Positivas

- El plazo es variable por infoproductor y puede tener overrides específicos.
- Cada timer conserva el número y la versión que lo originaron.
- Compra posterior y vencimiento se resuelven contra estado autoritativo.
- El sistema sobrevive reinicios sin cronjobs por persona.
- La fase no amplía autoridad comercial ni crea contactos implícitos.

### Costos

- Se necesita una cola pre-comercial adicional porque el aggregate comercial
  existente exige `contact_id`.
- La promoción hacia `scheduled_actions` sigue pendiente hasta tener un binding
  canónico y todas las guardas de autorización.
- La asignación de política requiere una operación administrada y auditable.

## Alternativas descartadas

### Hardcodear un número en el runtime

Descartado porque no permite preferencias por infoproductor, no es versionado y
cambia el comportamiento al redesplegar.

### Usar una variable de entorno global

Descartado como fuente de verdad: no soporta múltiples infoproductores ni
preserva el valor aplicado a timers anteriores.

### Crear inmediatamente un `scheduled_action`

Descartado porque no existe un `contact_id` canónico y el scheduler exige ese
binding. Relajar sus foreign keys o inventar un contacto degradaría invariantes.

### Crear un cronjob por intención

Descartado por operabilidad, idempotencia y supervivencia a despliegues. El
worker consulta una cola durable en Postgres.
