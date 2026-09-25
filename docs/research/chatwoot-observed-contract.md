# Contrato observado de Chatwoot 4.13.0

## Alcance

Este documento registra la estructura observada en una prueba real del flujo WhatsApp → Evolution API 2.3.7 → Chatwoot 4.13.0 → bridge. Los valores personales, identificadores del remitente, contenido de mensajes, tokens y payloads completos no se incluyen.

La evidencia provino de:

- un webhook real `message_created` público y entrante aceptado por el bridge;
- `GET /api/v1/accounts/{account_id}/conversations/{conversation_id}`;
- `GET /api/v1/accounts/{account_id}/conversations/{conversation_id}/messages`;
- un mensaje saliente enviado manualmente desde Chatwoot y observado después mediante la API.

## Referencias opacas disponibles

El webhook permite extraer referencias numéricas estables sin copiar entidades canónicas completas:

| Referencia | Campo observado | Tipo |
|---|---|---|
| Cuenta | `account.id` | integer |
| Inbox | `inbox.id`, `conversation.inbox_id` | integer |
| Contacto | `sender.id`, `conversation.contact_inbox.contact_id` | integer |
| Contacto en inbox | `conversation.contact_inbox.id` | integer |
| Conversación | `conversation.id` | integer |
| Mensaje | `id` | integer |

`conversation.contact_inbox.source_id` y `conversation.meta.sender.identifier` son strings externos. El segundo contiene el identificador real del remitente usado por la integración Evolution/WhatsApp y es el valor autorizado por el filtro actual. No debe registrarse en logs ni copiarse a documentación.

## Webhook entrante

El evento observado expuso:

- `event = "message_created"`;
- `message_type = "incoming"` en el nivel superior;
- `private = false`;
- `conversation.status`;
- `conversation.can_reply`;
- `conversation.labels`;
- `conversation.custom_attributes`;
- `conversation.meta.assignee` y `conversation.meta.team`;
- `conversation.snoozed_until`;
- `conversation.contact_inbox`;
- `conversation.messages`, que incluyó nuevamente el mismo mensaje del evento.

La duplicación del mensaje dentro de `conversation.messages` no debe interpretarse como una entidad adicional ni persistirse en Supabase.

## Consulta de conversación

El endpoint verificado fue:

```text
GET /api/v1/accounts/{account_id}/conversations/{conversation_id}
```

La respuesta observada permitió reconstruir estado canónico actual de Chatwoot:

- estado y posibilidad de responder;
- inbox;
- etiquetas y atributos personalizados;
- mute y snooze;
- último mensaje no de actividad;
- timestamps de actividad;
- información del remitente;
- mensaje reciente incluido en la conversación.

`unread_count` y `agent_last_seen_at` cambiaron entre el webhook y la consulta posterior. Son estado de interfaz/lectura y no deben usarse como estado comercial, control de concurrencia ni evidencia suficiente de intervención humana.

## Show de una conversación por la API (observado 2026-09-24)

Capturado el 2026-09-24 12:38 UTC desde el contenedor del bridge con el token de control, inbox 9 (WhatsApp Cloud), conversación 158; fixture saneado en `tests/fixtures/chatwoot_conversation_api_show_conv_158_20260924.json`. Es la forma que consumen el monitor de conversaciones estancadas y `get_canonical_conversation_snapshot`, y **difiere del webhook `message_created`** en la identidad del contacto:

| Campo | Webhook `message_created` | `GET /api/v1/accounts/{account}/conversations/{id}` |
|---|---|---|
| `conversation.contact_inbox.source_id` | presente (dígitos WABA) | **ausente en la raíz**; sólo aparece anidado en `messages[].conversation.contact_inbox` y en `last_non_activity_message.conversation.contact_inbox` |
| `meta.sender.identifier` | `null` en WhatsApp Cloud | `null` |
| `meta.sender.phone_number` | E.164 | E.164 (única identidad disponible en la raíz) |
| `meta.sender.type` | `contact` | **ausente** (el `type` sí viene en `messages[].sender`) |
| `meta.assignee` / `meta.team` | presentes | **ausentes cuando no hay asignado** (`meta` trae `channel`, `hmac_verified`, `sender`) |
| `labels` | lista de strings | lista de strings |
| `can_reply` | booleano | booleano |

Confirmado de nuevo el 2026-09-25 21:54 UTC sobre la conversación 177 (fixture saneado en `tests/fixtures/chatwoot_paused_lead_reply_inbox_9_conv_177_20260925.json`, que además trae el webhook del mensaje 2376 de la misma conversación). Consecuencia para todo lector del show: la identidad **hay que pasársela** (`expected_jid`) al cliente; si no, `_is_authorized_conversation` compara el teléfono contra `ALLOWED_WHATSAPP_JID` (el número de prueba) y falla con `conversation_identity_mismatch` para cualquier lead real. Así se cayó en silencio la reanudación de conversaciones pausadas entre el 23/09 y el 25/09 (ver `docs/contracts/resume-paused-conversation-v1.md`).
| `messages` | — | embebido en el show (los últimos), además del endpoint `/messages` que devuelve `{meta, payload}` |

Consecuencia: un clasificador que exige `identifier` o `contact_inbox.source_id` acepta el webhook y rechaza el show. Con los remitentes acotados por scope, el teléfono E.164 canónico se canoniza a `<dígitos>@s.whatsapp.net`, que es exactamente el valor que el webhook trae en `source_id`.

## Consulta del historial

El endpoint verificado fue:

```text
GET /api/v1/accounts/{account_id}/conversations/{conversation_id}/messages
```

La respuesta observada tuvo esta forma:

```text
{
  "meta": { ... },
  "payload": [ ...mensajes... ]
}
```

En la conversación de prueba, `payload` devolvió los mensajes observados en orden cronológico. Cada elemento incluyó ID, conversación, inbox, dirección, remitente, estado, privacidad, timestamp y contenido. La prueba no demostró todavía el comportamiento de paginación para historiales mayores al límite del endpoint.

Esto permite cargar contexto conversacional desde Chatwoot al construir una invocación de Hermes, sin mantener una tabla local `messages`.

## Dirección y actor observados

| Caso | `message_type` en API | Actor observado |
|---|---:|---|
| Mensaje entrante del prospecto | `0` | `sender.type = "contact"` |
| Mensaje saliente manual desde Chatwoot | `1` | `sender.type = "user"` |
| Mensaje saliente mediante AgentBot | `1` | `sender.type = "agent_bot"` |

El webhook representa la dirección entrante como string (`"incoming"`), mientras la API de mensajes usa el valor numérico `0`. El bridge debe normalizar ambos contratos explícitamente; no debe asumir que usan el mismo tipo.

## Intervención humana

Después de enviar manualmente un mensaje desde Chatwoot se observó:

- un nuevo mensaje con `message_type = 1`;
- actor `user`;
- la conversación permaneció `open`;
- `can_reply` permaneció en `true`;
- el assignee permaneció nulo;
- las etiquetas permanecieron vacías;
- mute y snooze permanecieron desactivados.

Por lo tanto, asignación, estado y etiquetas no detectan por sí solos una respuesta humana. Una regla basada únicamente en `assignee != null` produciría falsos negativos.

La decisión adoptada en
[`ADR-0002`](../decisions/0002-human-takeover-detection.md) es:

1. Enviar las futuras respuestas automáticas con un AgentBot nativo y exclusivo.
2. Configurar su ID como identidad de automatización autorizada.
3. Tratar todo mensaje público saliente de actor `user` como intervención humana.
4. Procesar esos eventos salientes de forma determinística para pausar o cancelar automatizaciones, sin invocar Hermes.
5. Reconciliar la misma regla consultando el historial de Chatwoot antes de ejecutar una acción programada.
6. Reflejar la pausa mediante la etiqueta `automation_paused` en Chatwoot,
   manteniendo allí la señal canónica visible para operadores.

## AgentBot saliente desvinculado

Se creó un AgentBot `webhook` de prueba con access token y secreto dedicados,
sin vincularlo al inbox API. La API aceptó su token para crear un mensaje
saliente en la conversación real existente. El mensaje:

- llegó al WhatsApp autorizado;
- quedó en estado `sent`;
- se registró con `message_type = 1`;
- expuso `sender.type = "agent_bot"`;
- expuso el ID estable del AgentBot;
- conservó el inbox y la conversación existentes.

La prueba demuestra que la arquitectura actual no necesita vincular el AgentBot
al inbox para usarlo como identidad saliente. El webhook general de cuenta puede
continuar como única entrada, evitando un segundo secreto activo, eventos
duplicados y el cambio automático de conversaciones nuevas a `pending`.

## Consecuencias para Supabase

El modelo operativo puede usar referencias opacas con nombres explícitos como:

- `chatwoot_account_id`;
- `chatwoot_inbox_id`;
- `chatwoot_contact_id`;
- `chatwoot_contact_inbox_id`;
- `chatwoot_conversation_id`;
- `chatwoot_message_id` o nombres causales más específicos, como `trigger_chatwoot_message_id` y `expected_chatwoot_message_id`.

No se justifica persistir copias completas de contactos, conversaciones ni mensajes. El contenido conversacional debe cargarse transitoriamente desde Chatwoot y limitarse a una sola conversación autorizada por invocación.

## Pendientes

- Verificar paginación y límites del endpoint de mensajes antes de definir cuánto historial cargar.
- Implementar la clasificación determinística de actores `contact`, `user` y
  `agent_bot`, validando además el ID configurado del AgentBot.
- Verificar la aplicación idempotente de `automation_paused` cuando el bridge
  procese eventos salientes humanos.
- Verificar los endpoints de búsqueda/creación de contactos y contact-inboxes cuando se incorpore una fuente externa que pueda iniciar un caso sin conversación previa.
