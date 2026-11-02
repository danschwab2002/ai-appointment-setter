# Rechazos de Meta y el único reintento del 131049 (meta-delivery-rejections-v1)

- **Estado:** contrato de la construcción (E-284, P-12a del plan del 2026-11-02). Todo apagado por defecto.
- **Decisiones de Dan:** D1 del 2026-11-02 (un solo reintento a las 24 h o más, solo el primer contacto, nunca en ráfaga) y el punto 5 de «lo que se prende en cada aliada» del 2026-11-02 (el 131026 cierra el caso sin reintentar y avisa al equipo).
- **Migración:** `20261103000500_meta_delivery_rejections_v1.sql`. **Código:** `src/bridge/meta_delivery.py`.

## El problema

Chatwoot acepta la plantilla (el intento queda `accepted_by_chatwoot` y el caso de un toque se cierra en `sequence_exhausted`) y después Meta la rechaza. Chatwoot marca el mensaje `status = failed` y guarda el código en `content_attributes.external_error` (`"131049: ..."`). El bridge no recibe `message_updated`: solo se suscribe `message_created` y `conversation_updated`. Hasta este contrato, la base no se enteraba.

## Qué se lee y de dónde

- **El historial** (`GET /conversations/{id}/messages`): `status`, `content_attributes` (las marcas del bridge y `external_error`) y `created_at`. No trae `additional_attributes`.
- **El show** (`GET /conversations/{id}`): el último mensaje con `additional_attributes.template_params` (`name`, `category`, `language`, `processed_params`). La plantilla se registra solo si el mensaje rechazado es ese último.
- **El tipo de toque** sale de la marca que el bridge le pone a cada plantilla: `recovery_first_touch_hash` (`first_touch`, o `purchase_welcome` con `flow_engine_purchase_welcome`, o `first_touch_retry` con `recovery_first_touch_retry_of`), `recovery_followup_hash` (`followup`), `reactivation_command_key` (`reactivation`) y `conversation_followup_command_key` (`coupon_followup`).
- **El código** son solo los dígitos del principio de `external_error` (como `automation_expert_read`). El texto no se guarda.

## El barrido

`MetaDeliveryRejectionSweeper`, cada `META_DELIVERY_SCAN_INTERVAL_SECONDS` (600 por defecto, entre 60 y 3600):

1. Lista las conversaciones de la bandeja (abiertas, resueltas, pendientes y pospuestas) con actividad en las últimas 6 h. El primer barrido de cada proceso, con el reintento prendido, mira 72 h.
2. Registra cada mensaje nuestro en `failed` con `record_meta_delivery_rejection_v1`: idempotente por (bandeja, mensaje), atado al intento (`followup_delivery_attempts.remote_message_id`), a su caso y a la conversación cuando la base los conoce. El intento y el caso no cambian.
3. Con el reintento prendido y dentro del horario, toma de la base los primeros contactos con 131049 de entre 24 h y 72 h sin reintento (`list_due_meta_first_touch_retries_v1`), relee cada conversación y decide.

## El reintento (solo 131049, solo el primer contacto)

Sale una sola vez, con la misma plantilla, los mismos parámetros y el mismo texto, en la misma conversación, por el AgentBot, con `recovery_first_touch_retry_of = <id rechazado>`. Hasta 3 por barrido.

**No sale** (y queda `blocked` con el motivo, para siempre) si:

- en Chatwoot: la persona escribió después (`bridge_lead_wrote`), hay otro mensaje después que no es de actividad (`bridge_conversation_moved_on`), la plantilla no está en `META_FIRST_TOUCH_RETRY_TEMPLATES` (`bridge_template_not_retryable`), no se pueden leer los parámetros, el teléfono o el texto, o el contacto está bloqueado;
- en la base (`claim_meta_first_touch_retry_v1`): pasó el techo (`blocked_too_late`), la conversación está tomada o cerrada (`blocked_conversation`), hay una derivación sin atender (`blocked_pending_handoff`), el contacto no permite contacto (`blocked_contact`), hay una baja por esa conversación o ese teléfono (`blocked_opt_out`), compró (`purchase_already_approved`: el predicado del seguimiento con cupón más un `PURCHASE_APPROVED`/`PURCHASE_COMPLETE` de Hotmart de los últimos 30 días por los últimos 9 dígitos o el mail), o el caso se cerró por otra cosa (`blocked_case_closed`: `won`, `lost`, `cancelled`, `unreachable`, `error`, `expired`, `escalated`).

**Espera** sin escribir si faltan 24 h (`too_early`), la conversación está pospuesta, el horario está cerrado o el efecto final de Meta está cerrado.

Si Chatwoot rechaza el envío, la reserva queda `failed` con el motivo y no se reintenta. Si el resultado es incierto, queda `claimed` y tampoco. El rechazo del reintento por Meta se registra como `first_touch_retry` y nunca vuelve a la lista.

## El 131026 (el número no recibe WhatsApp)

Con `META_UNREACHABLE_CLOSE_ENABLED`, el registro de un 131026 cierra el caso del intento como `unreachable` (`next_contact_reason = meta_131026_unreachable`), cancela sus acciones pendientes sin lease (`terminal_reason = meta_131026_unreachable`) y su secuencia, y pide el aviso al equipo. Un caso que ya terminó no se toca (`case_already_closed`); sin caso conocido, `no_case`. Nunca se reintenta.

El aviso es `MSG-002` («Envío falló definitivamente») por el conector de Slack de la aliada, el mismo productor que `MOT-002`: `reason_code = meta_131026_<tipo de toque>`, `component = chatwoot.conversation.<n>`, sin teléfono ni texto. El id es estable por (bandeja, mensaje): un aviso que no llegó a marcarse se vuelve a pedir y el conector lo deduplica. Sin conector, se registra y `/ready` lo cuenta (`notice_without_slack`).

## Las variables

| Variable | Default | Qué hace |
|---|---|---|
| `META_DELIVERY_REJECTIONS_ENABLED` | `false` | El barrido que registra. Solo lee Chatwoot y escribe en la base. |
| `META_FIRST_TOUCH_RETRY_ENABLED` | `false` | El reintento. Exige el registro, el manifiesto, `META_FINAL_EFFECT_ENABLED=true`, la lista y un horario. |
| `META_FIRST_TOUCH_RETRY_TEMPLATES` | vacío | Las plantillas de primer contacto que se reintentan, separadas por coma. |
| `META_FIRST_TOUCH_RETRY_SEND_HOURS` | vacío | `HH-HH` en la zona del manifiesto; sin ella, `CONVERSATION_FOLLOWUP_SEND_HOURS`. |
| `META_UNREACHABLE_CLOSE_ENABLED` | `false` | El cierre y el aviso del 131026. Exige el registro. |
| `META_DELIVERY_SCAN_INTERVAL_SECONDS` | `600` | El intervalo del barrido. |

`/ready` publica `meta_delivery_rejections` (estado del barrido), `meta_delivery_rejections_last_scan` (los conteos), `meta_first_touch_retry` y `meta_unreachable_close` (`enabled`/`disabled`), sin números de teléfono ni nombres.

## Contar por plantilla

`public.meta_delivery_rejection_counts_v1` (sin permisos para la API; se lee por psql de solo lectura): por bandeja, plantilla (`sin_plantilla` si no se pudo leer), tipo de toque y código, cuántos se rechazaron, cuántos se reintentaron, cuántos frenó una barrera, cuántos fallaron al mandar y cuántos reintentos rechazó Meta otra vez.
