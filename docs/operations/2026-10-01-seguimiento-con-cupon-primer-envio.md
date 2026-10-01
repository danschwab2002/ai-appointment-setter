# Seguimiento con cupón de Johanna: primer envío real y las conversaciones resueltas

- Fecha: 2026-10-01.
- Tipo: evidencia operativa. Describe lo medido el 2026-10-01 en solo lectura: el estado previo
  y el primer envío, entre las 12:28 y las 13:10 UTC (§1 a §4), y el deploy del PR #213 con su
  primer barrido, entre las 14:07 y las 14:24 UTC (§5). No es contrato ni arquitectura.
- Alcance: el seguimiento con cupón de Johanna (PR #203, contrato
  `docs/contracts/conversation-followup-discount-v1.md`) tal como corría en el bridge
  `f7dd227`, y el PR #213 (`d103405`), que le suma las conversaciones resueltas.
- Claims: `claude-followup-resolved-conversations-v1` (§1 a §4) y
  `claude-followup-resolved-evidence-v1` (§5).

Este documento no contiene teléfonos, nombres, emails, tokens, `fbclid` ni el valor del cupón.

## 1. Lo que corre

Leído el 2026-10-01 12:35 UTC dentro del contenedor del bridge:

- `GIT_SHA=f7dd22720327f27da7be86d79de51b2e1c6b3cad` (merge del PR #204), contenedor iniciado el
  2026-09-29 12:03:27 UTC. El PR #203 está en su historia.
- `CONVERSATION_FOLLOWUP_ENABLED=true`, plantilla `johanna_seguimiento_descuento_01`, idioma
  `en`.
- `/ready`: `conversation_followup = healthy`, último barrido
  `scanned=26 sent=0 never_replied=11 conversation_paused=11 inbound_too_recent=2
  blocked_followup_limit=1 other_reasons=1`.
- El log del contenedor no trae ninguna línea `conversation_followup_*`: el bridge no configura
  `logging` y los `logger.info` se pierden. El barrido se observa por `/ready` y
  `conversation_followup_events`.

## 2. El primer envío (E2E)

Conversación 212 del inbox 9, el 2026-10-01:

| Hecho | Dónde se leyó |
|---|---|
| Fila `sent` en `conversation_followup_events`, régimen `link_sent_no_purchase`, `inbound_age_seconds = 86631`, creada y cerrada a las 11:36 UTC | Supabase, Management API |
| Emisión del seguimiento `accepted_by_chatwoot` con `chatwoot_message_id = 2628`, `source_kind = precheckout_request`, formato de `sck` `v1` | `checkout_link_issuances` |
| Mensaje 2628: plantilla `johanna_seguimiento_descuento_01`, idioma `en`, un botón de URL, `status = read`, sin `external_error` | Base de Chatwoot |
| Ningún evento de Hotmart trae el `sck` de esta emisión ni el de la anterior a la misma conversación | `webhook_events`, hasta las 13:00 UTC |

### La URL del botón

Se reconstruyó con lo que Chatwoot guardó del envío (`template_params.processed_params.buttons[0]`)
y se contrastó con la emisión, el catálogo de ofertas y el primer link que el agente mandó a la
misma conversación (mensaje 2579, 2026-09-30 11:33 UTC). Los 21 chequeos dan bien:

- base `https://pay.hotmart.com/` y producto `F106691755G`, el del catálogo (`checkout_offer_catalog`,
  `funnel_ref = psicologajohanna`);
- `off=mgbgpp19`: la oferta de la intención del lead y la misma del link del agente;
- `checkoutMode=10` y `src=hermes`;
- `sck = <sck del anuncio>|hermes|v1|<ULID de la emisión del seguimiento>`: el mismo anuncio que
  el primer link, con un ULID propio, así una compra por el botón se atribuye al seguimiento;
- el mismo `fbclid` que el primer link;
- `offDiscount` al final, igual al cupón configurado en el bridge, al de la fila del seguimiento
  y al `{{3}}` del cuerpo;
- el sufijo es exactamente `checkout_url_final` de la emisión más `&offDiscount=<cupón>`, sin
  parámetros repetidos ni de más.

Chatwoot 4.13 pasa el sufijo sin tocarlo (`Whatsapp::PopulateTemplateParametersService#build_button_parameter`:
`{type: 'text', text: parameter.strip}`), y Meta lo aceptó.

El cupón aplica en esa oferta. Checkout real, Chrome headless con viewport móvil, sin `src`,
`sck` ni `fbclid` y con los píxeles bloqueados: con `offDiscount` el checkout muestra
«¡Se ha aplicado el cupón …!», «Cupón de descuento (10%)», el campo `#COUPON` visible y
completo, y el total pasa de $ 86.514,00 a $ 77.862,60 (ARS).

**No medido:** qué arma WhatsApp con el sufijo cuando la persona toca el botón (si conserva
`?`, `&` y `%7C`). Se cierra con un envío controlado de la misma plantilla a un número del
equipo.

## 3. Las conversaciones resueltas

El barrido pedía solo `status=open`. En el inbox 9 había 26 abiertas y 149 resueltas, y las
149 las resolvió a mano una persona del equipo entre el 2026-09-08 y el 2026-09-29, en tandas,
para ordenar su bandeja. Las actividades de sistema de Chatwoot lo registran como
`Conversation was marked resolved by <persona>`.

De las 11 conversaciones de los últimos 10 días que atendió solo el agente (el lead contestó,
el agente respondió último y la persona se calló), 8 están resueltas: 7 se resolvieron antes de
las 72 h del último mensaje del lead y 4 antes de las 24 h. En Supabase esas conversaciones
siguen `active`, sin derivación sin atender y sin opt-out.

Desde la activación (2026-09-28 23:57 UTC):

| Conversación | Qué pasó |
|---|---|
| 212 | Enviado (§2) |
| 184 y 193 | Resueltas a las 22:31 UTC del 2026-09-28, antes de la activación; seguían en la ventana |
| 201 | Resuelta 16 h después del último mensaje del lead, antes de entrar en la ventana |
| 173 | Bloqueada por la base: `paused_human` con derivaciones sin atender, como manda el contrato |
| 197 y 214 | Abiertas; entran en la ventana el 2026-10-01 a las 18:03 y 18:35 UTC |

### La API de Chatwoot

Leído con el token de control del bridge:

- `GET /conversations?status=resolved&inbox_id=9` devuelve 25 por página, ordenadas por
  `last_activity_at` descendente, con y sin `sort_by=last_activity_at_desc`.
- `meta` trae `mine_count`, `assigned_count`, `unassigned_count` y `all_count`, sin
  `current_page`.
- Resolver actualiza `last_activity_at`: la 201 tiene la marca de su resolución, 16 h después
  de su último mensaje.
- Un saliente no reabre una conversación resuelta (`Message#reopen_conversation` corta en
  `return unless incoming?`); un entrante la reabre como `open`, porque el inbox no tiene bot
  activo.

La captura anonimizada está en
`tests/fixtures/chatwoot_followup_resolved_conversations_inbox_9_20261001.json`.

## 4. El cambio

El PR #213 barre `open` y `resolved` con corte por `last_activity_at` (contrato, sección
"Como barre"). No cambia la base, las barreras ni la plantilla. Cuando se escribieron las
secciones 1 a 4 (13:10 UTC) no estaba desplegado; el deploy y su efecto están en §5.

## 5. Desplegado y E2E con una conversación resuelta

Medido el 2026-10-01 entre las 14:07 y las 14:24 UTC, en solo lectura.

### Lo que corre

- El PR #213 se mergeó a las 13:54:19 UTC con merge commit `d103405`, ancestro de `origin/main`.
- El bridge se redesplegó a las 14:07:29 UTC. El contenedor quedó `healthy`, sin reinicios, y
  declara `GIT_SHA=d103405101a1af9093c7fa16677893e8dba520a3`.
- Los hashes de objeto de `src/bridge/followup_discount.py`, `src/bridge/chatwoot.py` y
  `src/bridge/app.py` dentro del contenedor son iguales a los de Git en `d103405`.
- Las variables del seguimiento no cambiaron: `CONVERSATION_FOLLOWUP_ENABLED=true`, la misma
  plantilla y el mismo idioma. El log no trae advertencias nuevas.

### El primer barrido

`/ready` después del primer barrido:
`scanned=44 sent=1 never_replied=24 conversation_paused=12 inbound_too_old=3 inbound_too_recent=2 other_reasons=2`.
Antes del cambio veía 26 conversaciones (§1).

El resumen nombra solo los cuatro motivos más frecuentes, y `other_reasons` cuenta cuántos
motivos distintos quedaron afuera, no cuántas conversaciones (`_scan_summary` en
`src/bridge/reactivation.py`). Por eso los números no suman `scanned`.

### El envío a una conversación resuelta

Conversación 201 del inbox 9, que una persona del equipo había resuelto 16 h después del último
mensaje del lead (§3):

| Hecho | Dónde se leyó |
|---|---|
| Fila `sent` en `conversation_followup_events`, creada a las 14:09:30 UTC, régimen `link_sent_no_purchase`, `inbound_age_seconds = 210042` (58,3 h) | Supabase, Management API |
| Emisión `accepted_by_chatwoot` con `chatwoot_message_id = 2629`; su `checkout_url_final` tiene la forma `https://pay.hotmart.com/F106691755G?off=…&checkoutMode=10&src=hermes&sck=…` | `checkout_link_issuances` |
| Mensaje 2629: plantilla `johanna_seguimiento_descuento_01`, un botón de URL, `status = delivered` a las 14:24 UTC, sin `external_error` | Base de Chatwoot |
| La conversación sigue `resolved` después del envío | Base de Chatwoot |

La última fila confirma lo que dice el código de Chatwoot (§3): un saliente no reabre la
conversación. El equipo la sigue viendo resuelta y el lead recibe el mensaje.

### Releído a las 14:24 UTC

- Último barrido:
  `scanned=44 sent=0 never_replied=24 conversation_paused=12 inbound_too_old=3 blocked_followup_limit=2 other_reasons=2`.
  `blocked_followup_limit=2` son la 212 y la 201: la base admite un solo seguimiento por
  conversación (`20260928000400_conversation_followup_discount_v1.sql`), y son las dos únicas
  filas de `conversation_followup_events`.
- El mensaje 2628 de la 212 sigue `read`.

**No medido:**

- qué arma WhatsApp con el sufijo cuando la persona toca el botón (§2);
- si la 197 y la 214 reciben el suyo al entrar en la ventana, a las 18:03 y 18:35 UTC.
