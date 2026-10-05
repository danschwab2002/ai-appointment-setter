# Contrato — paquete identificado de revision diaria V2

- **Estado:** contrato de construccion; implementado en la rama del PR, sin desplegar
- **Version:** `daily-feedback-review-package-v2`
- **Decision:** `docs/decisions/0018-daily-feedback-identified-review-context.md`
- **Reemplaza en produccion a:** `daily-feedback-review-package-v1` (que sigue vigente para la superficie local de cuarentena)
- **Superficie:** Chatwoot → `ChatwootDailyCollector(package_version=2)` → `get_daily_feedback_conversation_context_v1` → `commit_daily_feedback_batch_v1` → pagina HTTPS autenticada

## 1. Versiones

| Campo | V1 | V2 |
|---|---|---|
| `schema_version` | `daily-feedback-review-package-v1` | `daily-feedback-review-package-v2` |
| `sanitizer_version` | `deterministic-redaction-v1` | `identity-preserving-redaction-v2` |
| `selection_version` | `chatwoot-daily-agent-dialogues-v1` | `chatwoot-daily-conversation-context-v2` |
| `renderer_version` | `daily-feedback-web-v1` | `daily-feedback-web-v2` |

El servicio `daily-feedback` declara las versiones V2 en `configure_daily_feedback_scope_v2` al arrancar. Cambiarlas invalida la ventana pendiente del schedule, como cualquier otro cambio de configuracion.

## 2. Seleccion (`chatwoot-daily-conversation-context-v2`)

Por conversacion del inbox configurado, actualizada dentro de la ventana, entran **todos** los mensajes cuyo `created_at` cae en `[window_start, window_end)`:

| Chatwoot | `actor` | `kind` |
|---|---|---|
| `message_type 0`, sender `contact` | `prospect` | `prospect_message` |
| `message_type 1`, sender `agent_bot` con el id configurado, `content_attributes.reactivation_command_key` | `agent` | `reactivation_template` |
| idem con `recovery_first_touch_hash` | `agent` | `first_touch_template` |
| idem con `recovery_followup_hash` | `agent` | `followup_template` |
| idem con una URL de `hotmart.com` en el texto | `agent` | `payment_link` |
| idem con `appointment_setter_reply_hash` | `agent` | `agent_reply` |
| idem sin marcador | `agent` | `agent_message` |
| `message_type 1`, sender `user`, publico | `team` | `team_message` |
| `message_type 1`, `private = true` | `team` | `handoff_note` si empieza con "Derivación", si no `private_note` |
| `message_type 2` (actividad) | `system` | `automation_paused`, `automation_resumed`, `label_added`, `label_removed`, `assigned`, `unassigned`, `conversation_resolved`, `conversation_reopened`, `conversation_open`, `conversation_pending`, `conversation_snoozed` o `activity` |
| `message_type 3` (template) | `agent` o `team` | `template_message` |

Un mensaje de otro `agent_bot` no entra. Un mensaje sin texto (adjunto, audio, sticker) entra con el texto `[sin texto: adjunto, audio o sticker]` y `meta.empty = true`. Los envios fallidos entran con su `status`.

Una conversacion es elegible si tiene **al menos un mensaje del prospecto** en la ventana. Ya no exige respuesta del agente.

Origen de los marcadores (capturados el 26/09/2026, `tests/fixtures/chatwoot_daily_feedback_messages_20260926.json`): el bridge publica respuestas con `appointment_setter_reply_hash` (`src/bridge/chatwoot.py`, `_authorize_and_send`), la reactivacion con `reactivation_command_key` (`send_reactivation_template`), el primer toque con `recovery_first_touch_hash` (`send_first_message`) y el seguimiento con `recovery_followup_hash` (`send_followup_message`).

## 3. Sanitizacion (`identity-preserving-redaction-v2`)

`minimize_review_text_v2` conserva nombres, telefonos, mails y URLs. Reemplaza:

- patrones de token, API key, bearer o secret por `[SECRETO REDACTADO]`;
- `javascript:`, `vbscript:` y `data:` por `[ESQUEMA BLOQUEADO]`;
- caracteres de control (salvo salto de linea) por `[CONTROL]`;
- espacios repetidos por uno; mas de una linea en blanco por una.

Es idempotente. El empaquetador (`_package_items`) y la restriccion `daily_feedback_messages_valid` (forma v2) lo exigen; un texto que no pase por el sanitizador no se confirma.

## 4. Forma del item

```json
{
  "conversation_ref": "conv_<hmac>",
  "display_label": "Conversación 01",
  "apparent_objective": "Consulta de precio o formas de pago",
  "observed_outcome": "Derivado a humano (explicit_human_request) · Automatización pausada",
  "release_id": "<sha256 del release del prompt>",  // o "release_lineage_unavailable"
  "release_version": 3,                            // o 0 si no se sabe
  "messages": [
    {"actor": "prospect", "kind": "prospect_message", "occurred_at": "2026-09-26T13:41:00Z", "status": "sent", "text": "…", "meta": {}},
    {"actor": "agent", "kind": "agent_reply", "occurred_at": "…", "status": "read", "text": "…",
     "meta": {"chatwoot_message_id": 2412, "decision": "handoff", "reason_code": "commercial_exception", "part": "1/3"}},
    {"actor": "agent", "kind": "payment_link", "occurred_at": "…", "status": "read", "text": "…",
     "meta": {"chatwoot_message_id": 2391, "attribution": "marker_only", "purchased": false, "offer_code": "bxjge6zq"}},
    {"actor": "team", "kind": "handoff_note", "occurred_at": "…", "status": "sent", "text": "Derivación inbound…", "meta": {"author": "Bridge Service"}},
    {"actor": "system", "kind": "automation_paused", "occurred_at": "…", "status": "sent", "text": "Bridge Service added automation_paused", "meta": {"actor_name": "Bridge Service", "label": "automation_paused"}}
  ],
  "context": {
    "chatwoot_conversation_id": 186,
    "conversation_url": "https://<chatwoot>/app/accounts/1/conversations/186",
    "contact": {"id": 200, "name": "…", "phone": "+57…", "email": "…"},
    "conversation": {"status": "open", "labels": ["automation_paused"], "assignee": null,
                     "created_at": "…Z", "first_reply_at": null, "last_activity_at": "…Z", "can_reply": true, "unread_count": 0},
    "origin": "inbound",
    "events": [{"kind": "handoff", "occurred_at": "…Z", "primary_reason_code": "commercial_exception", "detail_reason_code": "explicit_human_request", "requested_by": "agent", "status": "projected"}],
    "payment_links": [{"occurred_at": "…Z", "status": "accepted_by_chatwoot", "source_kind": "inbound_request", "chatwoot_message_id": 2391, "sck_value": "hermes~v1~…", "attribution": "marker_only", "checkout_url_final": "https://pay.hotmart.com/…", "purchased_at": null, "offer_code": "bxjge6zq", "landing_ref": "…"}],
    "prior_reviews": [{"local_date": "2026-09-25", "decision": "correct_with_feedback", "verbatim_feedback": "…", "decided_at": "…Z"}],
    "summary": {"prospect_messages": 3, "agent_messages": 3, "team_messages": 0, "agent_replied": true, "payment_link_sent": false, "purchase_recorded": false, "handoff_count": 1, "last_handoff_reason": "explicit_human_request", "reactivation_count": 0, "opt_out": false, "automation_paused": true, "status": "open", "last_actor": "team", "last_message_at": "…Z"}
  }
}
```

Restricciones durables (migracion `20260927000100`):

- `messages`: arreglo de 1 a 1000 elementos; cada elemento tiene exactamente las claves `actor, kind, meta, occurred_at, status, text` (forma v2) **o** `actor, occurred_at, text` (forma v1, que sigue aceptandose); `actor` en `prospect | agent | team | system`; `kind` `^[a-z][a-z0-9_]{0,63}$`; `status` `^[a-z][a-z0-9_]{0,31}$`; `meta` objeto de hasta 4000 caracteres; `text` de 1 a 4000 caracteres sin control (salvo salto de linea) ni bearer tokens.
- `context`: objeto de hasta 32 KB con claves dentro de `chatwoot_conversation_id, conversation_url, contact, conversation, origin, events, payment_links, prior_reviews, summary, agent_release`; `conversation_url` https; sin `javascript:` ni bearer tokens.
- `apparent_objective` y `observed_outcome`: 1 a 300 caracteres; `display_label` 1 a 80.

`commit_daily_feedback_batch_v1` acepta items con o sin `context` (sin `context` guarda `{}`). `get_daily_feedback_review_page_v1` devuelve `item.context`.

## 5. Objetivo aparente y resultado observado

Son derivados, no hechos: `derive_apparent_objective` mira primero la decision del agente (`send_payment_link` o un mensaje `payment_link` → "Pedido del enlace de pago") y despues palabras del lead (precio/cuotas, acceso/contraseña, info/programa). `derive_observed_outcome` lista, del mas fuerte al mas debil: compra registrada, link enviado, derivado a humano (motivo), opt-out, reactivaciones, atendida por el equipo, sin respuesta del agente / esperando al prospecto / el prospecto escribio ultimo, automatizacion pausada, conversacion resuelta.

## 6. Contexto durable: `get_daily_feedback_conversation_context_v1`

```text
get_daily_feedback_conversation_context_v1(
  p_tenant_ref text, p_scope_ref text,
  p_chatwoot_account_id bigint, p_chatwoot_inbox_id bigint,
  p_conversation_ids bigint[]           -- hasta 500 ids de Chatwoot (display id)
) returns jsonb                          -- { "<id>": { handoffs, reactivations, resumes, opt_outs, payment_links, prior_reviews } }
```

- Exige que exista un `daily_feedback_schedules` para tenant/scope con el mismo account e inbox; si no, `daily_feedback_scope_not_configured`.
- `handoffs`: `human_handoff_requests` por account, inbox y `external_conversation_id` (50 mas recientes).
- `reactivations`: `conversation_reactivation_events` por `external_conversation_id`; `resumes`: `conversation_resume_events` idem. Estas dos tablas no llevan account/inbox: el aislamiento por tenant es el de la base (single-tenant administrado, ADR-0005).
- `opt_outs`: `contact_opt_out_events` por account, inbox y conversacion canonica.
- `payment_links`: `checkout_link_issuances` por account, inbox y conversacion, con `offer_code` y `landing_ref` del catalogo.
- `prior_reviews`: decisiones de lotes anteriores no purgados cuyo `context.chatwoot_conversation_id` coincide (10 mas recientes).
- `security definer`, `search_path` vacio; ejecutable solo por `service_role`.

El scheduler la llama una vez por lote con todos los ids; si la llamada falla, la recoleccion falla (`fail_daily_feedback_collection_v1`) y se reintenta. No se confirma un lote sin contexto.

## 7. Pagina de revision (`daily-feedback-web-v2`)

Ademas de lo que ya mostraba V1:

- tarjeta del lead: nombre, telefono, mail, primer contacto, estado, etiquetas, asignado, origen y el boton "Abrir en Chatwoot #id" (`target=_blank`, `rel=noopener noreferrer`); chips "compró" y "ya revisada ×N";
- hilo con horas en la zona del schedule (`DAILY_FEEDBACK_TIMEZONE`), saltos de mas de 30 minutos marcados, burbujas por actor (lead, agente, equipo), notas internas en caja punteada, actividades como filas centradas;
- en cada mensaje del agente: tipo (respuesta, plantilla de reactivacion, primer toque, seguimiento, link de pago), decision y reason code, estado de entrega, y para el link de pago la atribucion y si hubo compra;
- listas de eventos internos, links de pago y revisiones previas.

La CSP, las cabeceras, el script inline (hash fijo) y el formulario de decision no cambian. Todo texto que viene de Chatwoot o Supabase pasa por `html.escape`.

## 8. Bridge: decision del agente en el mensaje

`send_agent_bot_reply(..., agent_decision=, agent_reason_code=)` agrega `appointment_setter_decision` y `appointment_setter_reason_code` a `content_attributes` cuando la propuesta los trae (alfabeto `^[a-z][a-z0-9_]{0,63}$`; otro valor se omite). El link de pago sale con `agent_decision = send_payment_link`. No cambia el hash de idempotencia ni la verificacion de la respuesta de Chatwoot. Solo aplica a mensajes publicados despues del deploy del bridge; los anteriores se muestran sin decision.

## 9. Privacidad y retencion

Lo que cambia respecto de V1 esta en ADR-0018: la pagina muestra PII del lead a los cuatro revisores autenticados. No cambian: Slack recibe solo `REV-001`; retencion, purga y tombstones; RLS y ACL (`scripts/supabase_acl_inventory.sql`, `scripts/supabase_schema_inventory.sql`, `tests/sql/followup_engine/validate_acl_hardening.mjs`).

## 10. Orden de despliegue

1. `supabase db push` con `20260927000100` (acepta items v1 y v2: el servicio viejo sigue funcionando).
2. Redeploy de `infra_daily-feedback` (declara versiones V2; el proximo lote sale identificado).
3. Redeploy de `infra_appointment-bridge` (estampa decision y reason code en cada respuesta nueva).

Evidencia del primer lote V2 real: pendiente hasta el E2E (se documenta en `docs/operations/` cuando exista).

## La procedencia del prompt (desde 2026-09-28)

⚠ **`release_id` y `release_version` cambiaron de significado.** Existian en el
esquema desde `20260910000100` y se escribian como los literales
`'release_lineage_unavailable'` y `0`; habia incluso un guard que lo **exigia**
asi. Desde la migracion `20260928000100` llevan el digest del release del prompt
y su ordinal, y el guard pasó a ser `review_package_release_lineage_invalid`:
admite el marcador de "no se sabe" o un digest de 64 hex con version ≥ 1, y nada
mas.

El detalle viaja en `context.agent_release`:

```json
"agent_release": {
  "release_digest": "<64 hex>",
  "release_ordinal": 3,
  "confidence": "verified",
  "model_requested": "agente-comercial",
  "model_answered": "glm-5.2",
  "bridge_release": "246de1ba",
  "context_builder_version": "shadow-context-v1",
  "context_digest": "<64 hex>",
  "turn_occurred_at": "2026-09-26T14:07:00Z"
}
```

**`confidence` es el campo que no se puede ignorar.** El registrador del perfil
corre por cron, no por turno, asi que la atribucion no se afirma: `verified` = el
prompt cambio despues del turno; `misattributed` = ya habia cambiado antes, o sea
que la version que se muestra no es de fiar; `open` = todavia no hay observacion
posterior; `no_release` = el registrador no habia corrido.

El texto del SOUL **no** entra al paquete: son 19 KB por release y vive en
`agent_prompt_releases`. Contrato completo:
[agent-prompt-provenance-v1](agent-prompt-provenance-v1.md) ·
[ADR-0020](../decisions/0020-agent-prompt-provenance.md).

Una conversacion sin turno registrado se queda con el marcador, no con el release
de otra: la lectura falla blando y el informe sale igual.
