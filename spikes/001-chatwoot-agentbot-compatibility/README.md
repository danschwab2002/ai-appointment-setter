# 001: Compatibilidad de AgentBot con el inbox Evolution

## Pregunta

**Given** Chatwoot 4.13.0 self-hosted y un inbox `Channel::Api` usado por Evolution API, **when** se crea un AgentBot desvinculado y se usa su token para enviar a una conversación existente, **then** el mensaje debe llegar al WhatsApp autorizado y quedar distinguible de un agente humano sin cambiar el flujo entrante.

## Riesgos evaluados

- Migraciones de Chatwoot 4.13.0 incompletas podían impedir crear AgentBots.
- Vincular un AgentBot puede cambiar conversaciones nuevas a `pending`.
- Un webhook dedicado de AgentBot introduciría otro secreto y posibles eventos duplicados.
- Un token de bot desvinculado podía no tener permiso para enviar.
- Chatwoot podía registrar el mensaje automático como un usuario humano.

## Método

1. Se consultó `GET /api/v1/accounts/{account_id}/agent_bots` con una credencial existente.
2. Se verificó que el inbox API no tenía AgentBot vinculado.
3. Se creó un AgentBot `webhook` sin asociarlo al inbox.
4. Se confirmó que Chatwoot generó access token y secreto dedicado.
5. Se envió un único mensaje controlado mediante `POST /api/v1/accounts/{account_id}/conversations/{conversation_id}/messages`, autenticado con el token del AgentBot.
6. Se inspeccionaron únicamente metadatos sanitizados y se confirmó la recepción en el WhatsApp autorizado.

## Evidencia

- Endpoint de AgentBots disponible.
- AgentBot creado con ID estable, token y secreto.
- Inbox sin AgentBot vinculado antes y después de la prueba.
- API de mensajes respondió con estado `sent`.
- El mensaje se registró como `message_type = 1` y `sender.type = "agent_bot"`.
- El mensaje llegó al WhatsApp autorizado.
- La prueba humana previa registró `sender.type = "user"`.

No se almacenaron en este artefacto tokens, secretos, JID, teléfonos ni contenido conversacional personal.

## Verdict: VALIDATED

### What worked

- AgentBot funciona en la instalación self-hosted 4.13.0.
- Puede enviar a una conversación existente sin vincularse al inbox.
- Su actor es inequívocamente distinto de un usuario humano.
- El flujo Evolution → WhatsApp entregó el mensaje real.

### What didn't

- No se encontró ninguna incompatibilidad en el camino probado.
- No se probó ni se necesita por ahora el webhook dedicado del AgentBot.

### Surprises

- La vinculación al inbox no es necesaria para usar AgentBot como identidad saliente.
- Esto permite mantener el webhook general actual y evita adoptar la semántica `pending/open` de AgentBot.

### Recommendation for the real build

- Mantener el AgentBot desvinculado.
- Usar su access token exclusivamente para envíos del appointment setter.
- Conservar el webhook general de cuenta como única entrada.
- Clasificar `user` como humano y `agent_bot` con el ID configurado como automatización propia.
- Tratar actores salientes desconocidos de forma cerrada.
- Mantener el token sólo en secretos del despliegue.
