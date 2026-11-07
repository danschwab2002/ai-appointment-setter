# Rechazos de Meta y el único reintento del 131049 (meta-delivery-rejections-v1)

- **Estado:** contrato de la construcción (E-284, P-12a del plan del 2026-11-02). Todo apagado por defecto.
- **Decisiones de Dan:** D1 del 2026-11-02 (un solo reintento a las 24 h o más, solo el primer contacto, nunca en ráfaga) y el punto 5 de «lo que se prende en cada aliada» del 2026-11-02 (el 131026 cierra el caso sin reintentar y avisa al equipo).
- **Migración:** `20261103000500_meta_delivery_rejections_v1.sql`; el rechazo de la cuenta, `20261107000100_meta_account_hold_retry_v1.sql` (E-348). **Código:** `src/bridge/meta_delivery.py`.

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

## Cuando la cuenta no entrega (E-348, P-33)

- **Decisiones de Dan del 2026-11-07** sobre el diseño E-345: D1 solo el 131042 al arrancar, con la lista abierta; D2 vence a las 72 h del mensaje original; D3 solo los flujos de un mensaje (la misma lista que el 131049); D4 nada retroactivo; D5 la señal de «la cuenta volvió» es una plantilla nuestra entregada o leída después del último rechazo, y sin tráfico una sonda cada 2 h (ver «Lo que no está»); D6 aviso cuando se libera la cola.
- **Por qué:** el 04/11 Maribel tuvo un problema de pago con Meta y desde las 15:00Z todas las plantillas volvieron con `131042: Business eligibility payment issue`. Los casos de un toque se cerraron en `sequence_exhausted` como si hubieran llegado. Con el problema de pago Meta rechaza **las plantillas**; las respuestas dentro de la ventana de 24 h salen igual. Por eso la señal es una plantilla, no cualquier mensaje.

**La espera.** Con `META_ACCOUNT_HOLD_RETRY_ENABLED`, cada barrido empieza con el latido `mark_meta_account_hold_enabled_v1(bandeja, desde, ahora, hueco)`: `desde` es la hora de arranque del proceso del bridge y `hueco`, dos intervalos de barrido más 10 minutos (entre 300 y 86400 s). Sin fila en `meta_account_hold_activations`, la marca de prendido pasa a ser `desde` (`marked`). Si el último latido es más viejo que el hueco (la espera estuvo apagada o el bridge caído), la marca se mueve a `desde`, nunca hacia atrás (`remarked`). Si no, solo se mueve el latido (`beat`). Si el latido falla, ese barrido no pone nada en espera y lo repite en el siguiente; como la marca es la hora de arranque, lo rechazado mientras tanto entra igual. Cada primer contacto rechazado con un código de `META_ACCOUNT_HOLD_CODES`, registrado como `recorded` o como `replayed`, pasa por `hold_meta_account_rejection_v1`, y la fila entra a `meta_account_holds` en `waiting`, con `expires_at` = hora del mensaje + `META_ACCOUNT_HOLD_MAX_AGE_SECONDS`, **solo si el mensaje es posterior a la marca** (D4: si no, contesta `before_enabled`; sin marca, `not_enabled`). La base rechaza el 131049 y el 131026 (tienen su camino), los otros tipos de toque (`not_eligible`) y lo que ya venció (`too_late`). Una respuesta final se recuerda en el proceso y no se vuelve a pedir. Un paso de la espera que falla (latido, poner en espera, liberar, reintentar) no corta el barrido: el registro, el aviso del 131026 y el reintento del 131049 siguen, el error se cuenta como `hold_error` y el paso se repite en el barrido siguiente. Tras 3 barridos seguidos con algún `hold_error`, `/ready` publica `meta_account_hold_retry = degraded`, y `meta_account_hold_last_error` dice el paso y el tipo de error (`mark:SupabaseError`); un barrido limpio lo vuelve a `enabled` y `none`.

**Orden de activación.** Se puede prender `META_ACCOUNT_HOLD_RETRY_ENABLED` junto con `META_DELIVERY_REJECTIONS_ENABLED` o después: el primer barrido registra los rechazos de las últimas 72 h, pero ninguno anterior a la marca entra en espera. Apagarlo y volver a prenderlo tampoco es retroactivo: sin latido por más que el hueco, la marca pasa a la hora de arranque del proceso que lo volvió a prender.

**La señal.** En cada barrido el bridge guarda la hora de la última plantilla nuestra (con la marca del bridge, ver «Qué se lee») en `delivered` o `read` que vio en la bandeja; la recuerda aunque la conversación salga de la ventana. `release_meta_account_holds_v1` primero vence lo que pasó `expires_at` (`expired`, no sale nunca) y, si esa hora es posterior al último rechazo con un código de la cuenta en la bandeja (de cualquier tipo de toque, el reintento incluido), pasa las filas en `waiting` anteriores a la señal a `released`. Con al menos una liberada, el equipo recibe `SYS-008` («Recuperación confirmada») por el conector de Slack de la aliada: `reason_code = meta_account_hold_released`, `component = chatwoot.inbox.<n>`, `count` = cuántas salen, sin teléfonos ni textos. El id es estable por (bandeja, hora de la señal). Sin conector, se cuenta (`hold_notice_without_slack`).

**El reintento.** Dentro del horario del reintento del 131049 (`META_FIRST_TOUCH_RETRY_SEND_HOURS` o, sin ella, `CONVERSATION_FOLLOWUP_SEND_HOURS`) y con el efecto final de Meta abierto, hasta 3 por barrido: `list_due_meta_account_hold_retries_v1`, la relectura de la conversación con el mismo criterio que el 131049 pero sin la espera de 24 h, `claim_meta_account_hold_retry_v1` (las mismas barreras de la base: `blocked_too_late`, `blocked_conversation`, `blocked_pending_handoff`, `blocked_contact`, `blocked_opt_out`, `purchase_already_approved`, `blocked_case_closed`; más dos propias, por la misma persona en dos conversaciones: `blocked_same_person` si otra fila de la bandeja con los mismos últimos 9 dígitos, de la misma caída (sus vigencias se solapan), ya está `sent`, o `claimed` hace menos de una hora (una reserva vieja trabada no frena); y `blocked_newer_contact` si después de este mensaje Chatwoot aceptó otro contacto nuestro a ese teléfono que Meta no rechazó, hace más de 2 h; con menos, Meta todavía puede rechazarlo y la reserva contesta `newer_contact_pending` sin escribir (la fila sigue liberada); en espera contesta `not_released`), el envío de la misma plantilla con los mismos parámetros y `recovery_first_touch_retry_of`, y `settle_meta_account_hold_retry_v1`. Solo las plantillas de `META_FIRST_TOUCH_RETRY_TEMPLATES` (D3). En Chatwoot frena, para siempre, lo mismo que el 131049 (`block_meta_account_hold_retry_v1` con `bridge_<motivo>`), salvo que un reintento propio de ese mensaje rechazado otra vez no cuenta como «la conversación siguió»: los parámetros se leen de él. El id de entrega es `meta-hold:<bandeja>:<mensaje>:<intento>`.

**Si vuelve rechazado.** Con un código de la cuenta, `requeue_meta_account_hold_v1` devuelve la fila a `waiting` y hace falta una señal nueva, posterior a ese rechazo. En la misma pasada, `release_meta_account_holds_v1` devuelve a `waiting` las filas `released` cuya señal es anterior a ese rechazo (`held_back`), sin gastarles intentos: si la cuenta volvió a caer, el resto de la cola no sale. La reserva tiene la misma guarda, con los mismos códigos de la cuenta que la liberación (el bridge los pasa): con un rechazo de cualquiera de ellos posterior a la señal contesta `account_down_again` y devuelve la fila a la espera. Al tercer intento rechazado queda `blocked` con `hold_attempts_exhausted`. Con otro código (por ejemplo, 131049) la fila queda `sent`: se registra y se cierra ahí.

**Con el 131049.** Son filas distintas (una por mensaje, un código cada una): `list_due_meta_first_touch_retries_v1` no ve las de la cuenta y el reintento de la cuenta no exige que el del 131049 esté prendido.

**Lo que no está.** La sonda sin tráfico (D5: cada 2 h, máximo 3 por fila) no se construyó: medir la cuenta así es mandarle el mensaje real a la persona más vieja de la cola, y eso necesita la decisión explícita de Dan. Sin señal pasiva, la fila vence a las 72 h. La salud de la cuenta que avisa de la caída (P-32) vive en el OS (`meta_salud.py`), no en el bridge.

## Las variables

| Variable | Default | Qué hace |
|---|---|---|
| `META_DELIVERY_REJECTIONS_ENABLED` | `false` | El barrido que registra. Solo lee Chatwoot y escribe en la base. |
| `META_FIRST_TOUCH_RETRY_ENABLED` | `false` | El reintento. Exige el registro, el manifiesto, `META_FINAL_EFFECT_ENABLED=true`, la lista y un horario. |
| `META_FIRST_TOUCH_RETRY_TEMPLATES` | vacío | Las plantillas de primer contacto que se reintentan, separadas por coma. |
| `META_FIRST_TOUCH_RETRY_SEND_HOURS` | vacío | `HH-HH` en la zona del manifiesto; sin ella, `CONVERSATION_FOLLOWUP_SEND_HOURS`. |
| `META_UNREACHABLE_CLOSE_ENABLED` | `false` | El cierre y el aviso del 131026. Exige el registro. |
| `META_DELIVERY_SCAN_INTERVAL_SECONDS` | `600` | El intervalo del barrido. |
| `META_ACCOUNT_HOLD_RETRY_ENABLED` | `false` | El reintento del rechazo de la cuenta (E-348). Exige el registro, el manifiesto, `META_FINAL_EFFECT_ENABLED=true`, `META_FIRST_TOUCH_RETRY_TEMPLATES` y un horario; no exige `META_FIRST_TOUCH_RETRY_ENABLED`. |
| `META_ACCOUNT_HOLD_CODES` | `131042` | Los códigos de la cuenta, separados por coma, hasta 10. Nunca 131049 ni 131026. |
| `META_ACCOUNT_HOLD_MAX_AGE_SECONDS` | `259200` | El vencimiento desde el mensaje original (72 h), entre 3600 y 604800. |

`/ready` publica `meta_delivery_rejections` (estado del barrido), `meta_delivery_rejections_last_scan` (los conteos, con los `hold_*` del rechazo de la cuenta), `meta_first_touch_retry`, `meta_unreachable_close` (`enabled`/`disabled`), `meta_account_hold_retry` (`enabled`/`degraded`/`disabled`) y `meta_account_hold_last_error`, sin números de teléfono ni nombres.

## Contar por plantilla

`public.meta_delivery_rejection_counts_v1` (sin permisos para la API; se lee por psql de solo lectura): por bandeja, plantilla (`sin_plantilla` si no se pudo leer), tipo de toque y código, cuántos se rechazaron, cuántos se reintentaron, cuántos frenó una barrera, cuántos fallaron al mandar y cuántos reintentos rechazó Meta otra vez.

`public.meta_account_holds` (sin permisos para la API, también por psql de solo lectura): una fila por primer contacto en espera, con su estado (`waiting`, `released`, `claimed`, `sent`, `failed`, `blocked`, `expired`), los intentos, el motivo y la hora de la señal que lo liberó.
