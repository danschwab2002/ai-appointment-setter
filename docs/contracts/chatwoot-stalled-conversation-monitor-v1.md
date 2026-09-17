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
4. contacto no bloqueado e identidad WhatsApp válida dentro del scope;
5. historial canónico consultado desde el endpoint de mensajes;
6. el orden canónico de actividad se determina por el ID entero de mensaje de Chatwoot, no por `created_at`; esto mantiene el mismo criterio en el scanner y en el batching durable incluso con timestamps iguales o regresivos;
7. último mensaje público no vacío, inbound y enviado por el contacto;
8. edad dentro de la ventana configurada.

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