# Desvinculacion del AgentBot del inbox de Johanna (2026-09-23)

Cambio de configuracion en la instancia de Chatwoot de produccion
(`chatwoot/chatwoot:v4.13.0`, cuenta 1 "Johanna Psicologa", inbox 9 "WABA").
No cambia codigo del bridge. Este documento es la evidencia del cambio.

## El sintoma

En el inbox 9 aparecia repetidamente el mensaje de actividad de Chatwoot
"Conversation was marked open by system due to an error with the agent bot.".

Medido sobre `messages` el 2026-09-23T02:19Z: **61 apariciones en 61
conversaciones distintas**, la primera el 2026-09-11T19:17:30Z y la ultima el
2026-09-23T00:25:24Z. El inbox tiene 107 conversaciones en total, es decir que
el 57 % paso por esto. De esas 61, **16 terminaron con un mensaje escrito por un
humano** despues del error.

Las conversaciones 114, 123, 138 y 147 estan entre las afectadas. Son las mismas
en las que el equipo mando links de pago a mano.

## El mecanismo, medido en el codigo de Chatwoot

1. `app/listeners/agent_bot_listener.rb:85-90` encola `AgentBots::WebhookJob`
   contra `agent_bots.outgoing_url` en siete eventos: `message_created`,
   `message_updated`, `conversation_opened`, `conversation_resolved`,
   `conversation_status_changed`, `conversation_updated` y `webwidget_triggered`.
2. El `outgoing_url` configurado terminaba en `/webhooks/chatwoot/agent-bot`.
   **El bridge nunca expuso esa ruta**: sus endpoints son `/webhooks/chatwoot`,
   `/webhooks/lead`, `/webhooks/precheckout`, `/webhooks/hotmart` y
   `/webhooks/johanna-funnel-events`. La llamada devolvia `404 Not Found`,
   registrado en el log de `infra_chatwoot-sidekiq` como
   `Exception: Invalid webhook URL ... : 404 Not Found`.
3. `lib/webhooks/trigger.rb:16-24` captura el error. Un 429 o un 500 se
   relanzan para reintento, pero **cualquier otro error, el 404 incluido, cae en
   `handle_failure`**.
4. `lib/webhooks/trigger.rb:67-79`: si el evento es `message_created` o
   `message_updated` y la conversacion esta en `pending`, la pasa a `open` y
   escribe el mensaje de actividad. La unica salida seria
   `account.keep_pending_on_bot_failure`, que no esta activo.

De las 61 ocurrencias, 50 siguieron a un mensaje saliente del propio AgentBot y
11 a un entrante del contacto. Es decir: **cada vez que el agente respondia,
Chatwoot le pegaba a una URL inexistente y sacaba la conversacion de `pending`**.

## Por que el arreglo obvio era el peligroso

El primer arreglo evaluado fue vaciar `outgoing_url`, que corta el webhook en
`agent_bot_listener.rb:86` sin desvincular nada. Habria eliminado el mensaje,
pero **habria apagado el recuperador en silencio**:

- `app/models/conversation.rb:260-261` pone toda conversacion nueva en `pending`
  cuando `inbox.active_bot?`, que depende del vinculo `agent_bot_inboxes`, no del
  `outgoing_url`. Lo mismo hace `app/models/message.rb:424-427` cuando una
  conversacion resuelta revive por un mensaje del lead.
- El bridge **solo trabaja conversaciones `open`**: lista con `status=open`
  (`src/bridge/chatwoot.py:155`), descarta cualquier candidata cuyo estado no sea
  `open` (`src/bridge/chatwoot.py:262`) y revalida `conversation_not_open`
  inmediatamente antes de cada envio (`src/bridge/chatwoot.py:1102`).

Con el vinculo activo y sin el 404, las conversaciones se habrian quedado en
`pending`, que para el recuperador es invisible. El fallo del webhook estaba
**compensando** al vinculo: rompia el estado que el vinculo imponia.

`docs/research/chatwoot-observed-contract.md` ya lo habia anticipado: la prueba
documentada ahi concluye que la arquitectura no necesita vincular el AgentBot al
inbox para usarlo como identidad saliente, y que el webhook general de cuenta
puede seguir siendo la unica entrada, evitando un segundo secreto activo,
eventos duplicados y el cambio automatico de conversaciones nuevas a `pending`.

Hay un antecedente: el PR #156 (commit `2c80d82`, 2026-09-18) tuvo que ensenarle
al monitor de conversaciones estancadas a ignorar los mensajes de actividad. Ese
arreglo trato el sintoma de este mismo 404 sin llegar a la causa.

## El cambio aplicado

Dos mutaciones por `rails runner` dentro de `infra_chatwoot`:

| Cuando (UTC) | Que | Antes | Despues |
|---|---|---|---|
| 2026-09-23T02:18:30Z | `agent_bots.outgoing_url` (bot 1 "Appointment Setter") | `https://<host del bridge>/webhooks/chatwoot/agent-bot` | vacio |
| 2026-09-23T02:27:26Z | `agent_bot_inboxes.status` (fila 1, bot 1, inbox 9) | `active` | `inactive` |

Lo que **no** se toco: el AgentBot sigue existiendo con su access token, que es
lo que el bridge usa como identidad saliente (133 mensajes salientes con
`sender_type = AgentBot` en los ultimos 14 dias). El webhook de cuenta 1 contra
`/webhooks/chatwoot` sigue siendo la unica entrada. Ninguna conversacion fue
modificada.

## Verificacion

Estructural, el 2026-09-23T02:28Z:

| Chequeo | Resultado |
|---|---|
| `inbox.active_bot?` | `false` (era `true`) |
| `agent_bot_inboxes.status` | `inactive` |
| `agent_bots.outgoing_url` | vacio |
| `conversations.status` default | `0` = `open` |
| Conversaciones del inbox 9 en `pending` | `0` |
| Conversaciones con `assignee_agent_bot_id` | `0` |
| Conversaciones del inbox 9 | 19 `open`, 88 `resolved` |
| Mensajes de error nuevos desde el cambio | `0` |

Ninguna conversacion quedo atascada: no habia ninguna en `pending` al momento
del cambio.

**El bot sigue autorizado a escribir**, que es la unica forma en que este cambio
podria haber apagado el agente. `app/controllers/concerns/ensure_current_account_helper.rb:27-32`
autoriza a un AgentBot sobre una cuenta por dos caminos, y **los dos se cumplen**:
el bot 1 tiene `account_id = 1`, que es la cuenta de Johanna (linea 28, la que
decide), y la fila de `agent_bot_inboxes` **sigue existiendo** aunque este
`inactive`, asi que el fallback de la linea 29 tambien pasa. Por eso se opto por
`inactive` y no por borrar la fila. `InboxPolicy:24` ademas devuelve `true` para
cualquier AgentBot. Verificado sobre los datos reales de produccion, no por una
request HTTP con el token: el token del bot vive en el entorno del bridge y no se
leyo.

**Lo que falta, y por que:** no hubo trafico entrante entre el cambio y el cierre
de la jornada (el ultimo mensaje del inbox es de 2026-09-23T00:25:24Z). La
verificacion empirica se cumple con el proximo mensaje real: la conversacion
tiene que nacer en `open` y no debe aparecer ningun mensaje de actividad nuevo.
Se chequea con `recuperador_estado.py` en el vault del OS, que desde hoy alerta
si alguna conversacion vuelve a quedar en `pending`, si el vinculo se reactiva o
si el `outgoing_url` vuelve a tener valor.

## Como revertir

```ruby
AgentBotInbox.find_by(inbox_id: 9).update!(status: :active)
AgentBot.find(1).update!(outgoing_url: 'https://<host del bridge>/webhooks/chatwoot/agent-bot')
```

Revertir devuelve las dos consecuencias: conversaciones nuevas en `pending`
(invisibles para el recuperador) y un 404 por cada evento. Si alguna vez se
quiere usar el AgentBot como entrada real, el bridge tiene que exponer primero
una ruta para el, y hay que resolver que cada `message_created` llegaria dos
veces (el webhook de cuenta y el del bot traen `X-Chatwoot-Delivery` distintos,
asi que la deduplicacion por delivery id no los une) y que el bot firma con su
propio secreto, distinto del que valida `/webhooks/chatwoot`.
