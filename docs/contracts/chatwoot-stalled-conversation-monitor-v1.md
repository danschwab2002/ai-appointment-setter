# Contrato — monitor de conversaciones estancadas Chatwoot V1

- **Estado:** implementado y desplegado; default-off; pendiente de E2E controlado antes de la activación recurrente.
- **Objetivo:** recuperar mensajes inbound que no recibieron una respuesta porque el webhook, worker o AgentBot se interrumpió.

## Activación

`CHATWOOT_STALLED_MONITOR_ENABLED=false` por defecto. Al activarlo exige admisión Cut B, agente y respuestas automáticas activas, IDs canónicos de cuenta/inbox y uno de estos límites de identidad:

- `ALLOWED_WHATSAPP_JID` exacto; o
- `CHATWOOT_SCOPED_INBOUND_SENDERS_ENABLED=true`, que ya exige las barreras de opt-out, pausa humana y handoff.

Parámetros:

- intervalo de scan: 60 s;
- antigüedad mínima: 120 s;
- antigüedad máxima: 24 h;
- máximo de páginas: 5;
- cooldown para readmisión terminal: 300 s;
- máximo de admisiones de recuperación por mensaje: 3.

Todos son configurables por las variables `CHATWOOT_STALLED_*` declaradas en `.env.example` y `compose.yaml`.

## Criterio canónico

Un candidato sólo se admite cuando Chatwoot confirma en el momento del scan:

1. cuenta e inbox exactos;
2. conversación `open` y `can_reply=true`;
3. sin asignado humano y sin etiqueta `automation_paused`; Chatwoot puede representar la ausencia de asignado omitiendo `meta.assignee` o enviándolo como `null`, mientras cualquier valor no nulo excluye la conversación;
4. contacto no bloqueado e identidad WhatsApp válida dentro del scope; para el remitente exacto configurado, la identidad WABA puede provenir de `meta.sender.identifier`, `contact_inbox.source_id` o, cuando ambos están ausentes o son `null`, de `meta.sender.phone_number` en formato E.164 exacto; valores explícitos vacíos, booleanos o malformados no habilitan el fallback; con los remitentes acotados por scope (`CHATWOOT_SCOPED_INBOUND_SENDERS_ENABLED=true`) rige el mismo orden y, cuando no hay `identifier` ni `contact_inbox.source_id`, el teléfono E.164 canónico vale como identidad (se canoniza a `<dígitos>@s.whatsapp.net`), porque el show de la API (`GET /api/v1/accounts/{account}/conversations/{id}`) no trae `contact_inbox` en la raíz y `meta.sender.identifier` llega `null` en WhatsApp Cloud (fixture `tests/fixtures/chatwoot_conversation_api_show_conv_158_20260924.json`); un teléfono que no sea E.164 canónico sigue excluyendo la conversación;
5. historial canónico consultado desde el endpoint de mensajes;
6. el orden canónico de mensajes conversacionales se determina por el ID entero de Chatwoot, no por `created_at`; sólo mensajes públicos con un `message_type` entero conocido (0 inbound, 1 outbound o 2 actividad) son válidos, y los valores ausentes, booleanos, no enteros o desconocidos bloquean la candidatura; las actividades válidas (`message_type=2`) no participan en la selección, por lo que una actividad de sistema posterior no oculta el último mensaje conversacional; esto mantiene el mismo criterio en el scanner y en el batching durable incluso con timestamps iguales o regresivos;
7. último mensaje conversacional público no vacío, inbound y enviado por el contacto; un outbound conversacional posterior sigue excluyendo la conversación;
8. edad dentro de la ventana configurada.

El worker conserva las mismas reglas de tipo al revalidar inmediatamente antes de cada POST: ignora únicamente actividades públicas válidas de tipo 2 posteriores al inbound, bloquea cualquier inbound/outbound conversacional posterior y falla cerrado ante tipos públicos malformados o desconocidos. La revalidación se ejecuta antes y después de `pre_send_authorizer`.

Un outbound público posterior, una asignación humana, una pausa, un bloqueo, una identidad ambigua o un scan incompleto excluyen la conversación. La paginación falla cerrada si cambia `all_count`, no coincide `current_page` o el límite de páginas deja conversaciones sin revisar.

Inmediatamente antes de cada POST, el sender consulta la conversación canónica y exige el inbox exacto configurado, estado `open`, `can_reply=true`, contacto explícitamente no bloqueado, identidad autorizada, ausencia de asignado humano y ausencia de `automation_paused`, además de validar el historial. Si existe un gate durable previo al envío, lo ejecuta y después repite toda la revalidación canónica antes de reclamar el envío y hacer POST. Cualquier cambio bloquea el envío.

## Readmisión e idempotencia

El monitor crea la identidad estable `stalled-chatwoot:<conversation_id>:<message_id>` y la ingresa en el mismo `DurableChatwootInbox` del webhook. No existe un segundo pipeline de respuesta.

- Reinicios y scans repetidos no duplican una admisión activa o completada.
- Un trabajo terminal puede rearmarse sólo después del cooldown y hasta el límite configurado.
- El worker conserva sus verificaciones canónicas antes de invocar Hermes y antes de enviar; si la conversación avanzó, se asignó o fue pausada, no responde.
- La respuesta saliente mantiene la idempotencia existente por conversación, trigger y parte.

## Observabilidad

Cuando está desactivado, `/ready` publica `chatwoot_stalled_monitor=disabled`. Cuando está activado, readiness responde 503 hasta completar un scan íntegro y vuelve a 503 después de un error; en estado sano publica `chatwoot_stalled_monitor=healthy`. Los logs sólo incluyen tipo de error, nunca contenido ni identidad del contacto.

## Verificación pendiente

Antes de activar en producción: desplegar con el gate apagado, comprobar `/ready`, ejecutar un caso sintético nombrado con un inbound estancado, confirmar una sola respuesta y delta cero en conversaciones de control, y sólo entonces habilitar el monitor recurrente.
## Lo que salió mal la primera vez (2026-09-24 12:32 UTC)

- **Activación:** `CHATWOOT_STALLED_MONITOR_ENABLED=true` cargado en EasyPanel y desplegado el 2026-09-24 12:32:35 UTC sobre `5d7d901`, con `CHATWOOT_SCOPED_INBOUND_SENDERS_ENABLED=true`. Antes de prender se midió contra la base de Chatwoot qué debía admitir el primer barrido: exactamente una conversación (abierta, `can_reply`, sin etiqueta, sin asignado, último mensaje público del lead 13 h antes); otras tres cumplían todo menos la etiqueta `automation_paused`.
- **Lo que pasó:** el primer barrido completo tardó unos 70 s (unas 65 llamadas: listado + show + mensajes por cada conversación abierta del inbox), `/ready` respondió 503 dos veces al healthcheck de la imagen (intervalo 30 s, 3 reintentos: a la tercera Swarm habría reiniciado el contenedor) y a las 12:33:50 UTC publicó `chatwoot_stalled_monitor=healthy`. Cero candidatos, cero `chatwoot_stalled_monitor_candidate_failed`, ningún envelope `stalled-chatwoot:158:2233` en el inbox durable. La conversación esperada siguió sin respuesta.
- **La causa:** `_stalled_conversation_candidate` arma un payload con la forma del webhook a partir del **show de la API**, y ese show no trae `contact_inbox` en la raíz ni `meta.sender.identifier` (llega `null` en WhatsApp Cloud); la única identidad es `meta.sender.phone_number` en E.164. En modo scoped, `classify_chatwoot_event` exigía el JID desde `identifier` o `source_id` y devolvía `sender_not_allowed` para todas. Reproducido en local sobre el fixture capturado del 23/09 (conv 126) y del 24/09 (conv 158). Es la misma clase de defecto que el barredor de reactivación tuvo en su primer despliegue (`b46af00`): un criterio que descarta todo termina igual que uno que no tiene a quién atender, y los dos publican `healthy`.
- **Por qué los tests no lo vieron:** el único test del scanner con `identifier: null` corría con el JID exacto configurado igual al teléfono del fixture inventado, que es el camino del `ALLOWED_WHATSAPP_JID` de prueba y no el de producción. Ningún test corría el scanner en modo scoped sobre un show capturado.
- **Lo que cambia:** el clasificador, en modo scoped y sólo cuando no hay `identifier` ni `source_id`, acepta el teléfono E.164 canónico y lo canoniza al JID de dígitos (el mismo valor que `contact_inbox.source_id` trae en el webhook). El worker ya autorizaba ese fallback antes de enviar (`_is_authorized_conversation`) y el barredor de reactivación ya leía el teléfono; el monitor queda alineado con los dos. Tests nuevos sobre el fixture capturado, verificados en rojo sobre el código anterior.
- **Lo que no cambia todavía y queda anotado:** el monitor sigue atando `/ready` a su último barrido (503 antes del primero y tras cualquier error). El barredor de reactivación ya no lo hace por diseño; alinear el monitor es una decisión aparte.
