# Contrato — planificación de descuento post-inbound ATT1 V1

- **Estado:** Implementado localmente, default-off; migración y runtime productivo pendientes
- **Alcance:** exclusivamente `payment_failure` después de un primer contacto aceptado
- **Fuera de alcance:** activar la acción, enviar a Meta, publicar política o template, y conectar los otros triggers

## Entrada

`plan_commercial_ally_post_inbound_discount(...)` recibe:

- tenant, funnel y versión del binding;
- clave y versión exactas de política de descuento;
- Account ID, Inbox ID, Conversation ID y Message ID de Chatwoot;
- `external_user_id` canónico sin sufijo JID;
- timestamp del mensaje inbound.

El bridge sólo invoca la RPC después de que el webhook de Corte B valida y admite
durablemente el inbound. Para una conversación de recuperación ya habilitada,
este planner especializado se evalúa antes del alta genérica `inbound_sales`;
cualquier outcome terminal del planner cierra ese inbound sin abrir un caso genérico
ni invocar Hermes. El gate no puede coexistir con el agente de Corte B. El gate es:

```text
CHATWOOT_POST_INBOUND_DISCOUNT_PLANNING_ENABLED=false
COMMERCIAL_ALLY_DISCOUNT_POLICY_KEY=
COMMERCIAL_ALLY_DISCOUNT_POLICY_VERSION=
```

Habilitarlo requiere un manifiesto explícito del tenant `att1`, Supabase, Corte B y coincidencia exacta entre el scope Chatwoot del runtime y el manifiesto.

## Autoridad y elegibilidad

La RPC exige simultáneamente:

- binding comercial `active` y coincidente con Account/Inbox;
- scope del caso coincidente con tenant, producto Hotmart y offer del binding; compartir Account/Inbox no transfiere autoridad entre tenants;
- identidad WhatsApp activa `chatwoot:<account_id>`, Inbox ID en metadata y usuario exactos;
- conversación activa, automatización habilitada y sin takeover humano;
- caso `payment_failure` ligado a un scope `published` de `PURCHASE_CANCELED`;
- caso cerrado por agotamiento de la secuencia inicial;
- exactamente un `payment_failure_first_contact` en `accepted_by_chatwoot`, ligado
  a la misma conversación;
- exactamente un mensaje outbound aceptado correspondiente a esa acción;
- inbound posterior al mensaje inicial;
- política `published` y vigente con todos estos valores exactos:
  - porcentaje `10`;
  - `offer_expiration_mode = indefinite`;
  - `presentation_stage = later_step`;
  - `requires_inbound_reply_after_initial_template = true`;
  - `coupon_delivery_mode = meta_template_variable`;
  - `urgency_copy_allowed = false`;
  - `channel_provider = waba`;
  - `delivery_mode = approved_template`.

Una condición ausente devuelve un outcome `*_not_applicable` y no muta el caso.

## Efecto durable

En una sola transacción:

1. guarda un mensaje inbound canónico con contenido fijo `[redacted-inbound]` y metadatos sin cuerpo ni PII nueva;
2. abre una segunda secuencia de un mensaje, `reason = prospect_commitment`;
3. reabre el caso;
4. crea una acción `inbound_reply_offer` / `payment_failure_discount_offer`;
5. fija la política, cupón, template y mapeo de variable en `commercial_ally_post_inbound_discount_bindings` append-only;
6. registra un evento sanitizado.

La acción nace `deferred`, con `next_attempt_at = infinity` y `effect_authorized = false`. No es reclamable ni produce efectos. Un release posterior deberá validar el template aprobado y autorizarla explícitamente.

## Idempotencia

- `recovery_case_id` es PK del binding: máximo una acción adicional por caso.
- La acción usa `commercial-ally:post-inbound-discount:<recovery_case_id>` como clave semántica.
- Replay del mismo mensaje o cualquier inbound posterior del mismo caso devuelve
  `already_exists` con la acción original, incluso si después se retira el runtime
  o la política, hay takeover, se deshabilita la automatización o termina el caso.
  La resolución usa tenant, política, Account, Inbox, conversación e identidad
  guardados de forma append-only; no reabre ni vuelve reclamable la acción.
- Sin inbound no se invoca la RPC y no existe acción.

## Salida

La RPC devuelve exactamente una fila:

```text
outcome
recovery_case_id | null
scheduled_action_id | null
inbound_message_id | null
```

`created` y `already_exists` requieren los tres IDs. Los outcomes no aplicables exigen IDs nulos. Respuestas incompatibles fallan cerradas en el cliente Python.

## Seguridad y compatibilidad

- Tabla de binding con RLS; sin lectura ni DML directo para roles API o `service_role`.
- Escritura sólo mediante RPC `SECURITY DEFINER` concedida a `service_role`.
- `UPDATE` y `DELETE` del binding se rechazan incluso para owner.
- El nuevo gate es `false` por defecto y no modifica Corte B, Hermes, dispatcher ni Meta cuando está apagado.
