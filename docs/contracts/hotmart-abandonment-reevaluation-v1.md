# Contrato V1: reevaluación durable de abandono Hotmart

- **Estado:** Implementado localmente; no desplegado ni activado
- **Fecha:** 2026-08-21
- **ADR:** [ADR-0014](../decisions/0014-configurable-abandonment-reevaluation-timer.md)
- **Alcance:** programación y reevaluación interna; cero outbound

## 1. Fuente del plazo

La duración se obtiene de una versión publicada de
`followup_policy_versions.grace_period` seleccionada por una asignación de
scope.

Precedencia, de mayor a menor especificidad:

```text
1. tenant_ref + funnel_ref + product_ref + offer_ref
2. tenant_ref + funnel_ref + product_ref
3. tenant_ref + funnel_ref
```

No se permite `offer_ref` sin `product_ref`. Una asignación específica
deshabilitada detiene la búsqueda y no cae al default general.

Semántica:

```text
sin asignación    → scheduling_disabled / cero timer
assignment.enabled=false → scheduling_disabled / cero timer
policy no publicada o inválida → error fail-closed / cero timer
```

Rango:

```text
60 <= delay_seconds <= 2_592_000
whole_seconds(delay)=true
delay < policy.expires_after
```

## 2. Asignación activa

```text
AbandonmentTimerPolicyBinding
  binding_id: uuid
  tenant_ref: text
  funnel_ref: text
  product_ref?: text
  offer_ref?: text
  enabled: boolean
  policy_key: text
  policy_version: integer
  generation: bigint
  activated_at: timestamptz
  updated_at: timestamptz
```

Invariantes:

- una asignación corriente por scope exacto;
- `generation > 0` y aumenta en cada cambio;
- el target es una policy publicada `cart_recovery`;
- el cambio queda registrado sin PII en historia append-only;
- tablas sin DML para `anon`, `authenticated` ni `service_role`;
- la configuración se administra fuera del webhook.

## 3. Timer durable

```text
HotmartAbandonmentReevaluation
  id: uuid
  purchase_intent_id: uuid
  source_webhook_event_id: uuid
  source_scope_id: uuid
  policy_binding_id: uuid
  policy_binding_generation: bigint
  policy_key: text
  policy_version: integer
  delay_seconds_snapshot: integer
  observed_at: timestamptz
  due_at: timestamptz
  status: scheduled | completed
  outcome?: cancelled_purchased
          | blocked_not_authorized
          | blocked_contact_binding_missing
          | cancelled_intent_changed
  idempotency_key: text
  completed_at?: timestamptz
  created_at: timestamptz
  updated_at: timestamptz
```

Invariantes físicas:

- FK a intención, correlación/evento, scope, binding y policy;
- un timer por evento correlacionado;
- una clave de idempotencia única;
- `due_at = observed_at + delay_seconds_snapshot`;
- el snapshot cumple el rango contractual;
- `scheduled` no tiene outcome/completed_at;
- `completed` tiene outcome/completed_at;
- identidad y snapshot son inmutables;
- ningún rol API tiene DML directo.

## 4. Programación

Precondición completa:

```text
webhook source=hotmart
webhook event_type=PURCHASE_OUT_OF_SHOPPING_CART
correlation outcome=resolved
correlation purchase_intent_id=<intent>
correlation manual_handoff_required=false
intent lifecycle_state=waiting_for_purchase
intent current_classification=confirmed_abandonment
scope coincide exactamente con intent
binding seleccionada y válida
```

Resultado:

```text
binding ausente/deshabilitada → outcome=scheduling_disabled, timer_id=null
binding válida, primera vez   → outcome=scheduled, timer_id=<uuid>, created=true
replay idéntico, timer activo → outcome=scheduled, mismo timer_id, created=false
replay idéntico, ya terminal  → outcome=<terminal persistido>, mismo timer_id, created=false
```

Ningún replay cambia `due_at`, policy, binding generation ni delay snapshot.

Resultados no resueltos crean cero timers.

## 5. Compra posterior

Cuando `PURCHASE_APPROVED` resuelve la misma intención, la transacción deja la
intención en `purchased` y completa cualquier timer `scheduled` como:

```text
status=completed
outcome=cancelled_purchased
```

La operación es idempotente. Como excepción explícita, una compra resuelta puede
superseder un outcome terminal anterior sin efecto externo y cambiarlo a
`cancelled_purchased`; esa transición agrega evidencia de auditoría. Una vez en
`cancelled_purchased`, el outcome no vuelve a cambiar.

## 6. Listado y reevaluación

La consulta de vencidos retorna sólo IDs internos:

```text
list_due_hotmart_abandonment_reevaluations(now, batch_size)
→ reevaluation_id[]
```

No retorna email, teléfono, nombre ni payload.

Límites:

```text
1 <= batch_size <= 100
```

La RPC de reevaluación recibe `reevaluation_id` y `now` explícitos. Bloquea:

```text
purchase_intent → reevaluation
```

y relee ambos estados.

Resultados:

| Estado autoritativo | Resultado terminal |
|---|---|
| `lifecycle_state=purchased` | `cancelled_purchased` |
| `waiting_for_purchase + confirmed_abandonment` y alguna autorización local es falsa | `blocked_not_authorized` |
| ambas autorizaciones locales verdaderas, sin binding comercial canónico | `blocked_contact_binding_missing` |
| cualquier otro estado | `cancelled_intent_changed` |

Un timer no vencido es rechazado. Un replay de reevaluación sobre un timer
completado devuelve el outcome corriente sin escribir otro evento. La única
transición posterior permitida es la supersesión por una compra resuelta descrita
en la sección 5.

## 7. Runtime

Configuración:

```text
HOTMART_ABANDONMENT_TIMER_WORKER_ENABLED=false  # default
HOTMART_ABANDONMENT_TIMER_POLL_INTERVAL=5.0
HOTMART_ABANDONMENT_TIMER_BATCH_SIZE=10
```

Validación:

```text
poll_interval > 0 y finito
1 <= batch_size <= 100
worker enabled → Supabase configurado
```

El worker:

1. consulta IDs vencidos;
2. reevalúa cada ID;
3. registra sólo ID interno y outcome;
4. continúa ante errores Supabase por item;
5. no invoca Hermes, Chatwoot ni sender.

## 8. Fronteras no incluidas

Este contrato no habilita:

- creación de `contact`;
- binding de identidad a contacto;
- `recovery_case`;
- `scheduled_action` comercial;
- solicitud outbound;
- reserva de intento;
- Hermes;
- Chatwoot;
- WhatsApp, email o follow-ups.

Esas capacidades requieren un contrato posterior y las guardas completas de
ADR-0007.
