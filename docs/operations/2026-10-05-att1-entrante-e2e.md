# ATT1: el entrante en producción, con E2E

- Fecha: 2026-10-05.
- Tipo: evidencia operativa. Describe la activación del entrante de ATT1 y su E2E, medidos entre
  el 2026-10-05 11:18 UTC y las 12:10 UTC. No es contrato ni arquitectura.
- Alcance: la instancia de ATT1 (stack `setter-att1` del VPS) con el producto `v1.3.1`. **No toca
  a Johanna**: sus servicios `infra_*` no cambiaron de imagen ni de configuración.
- Claim: `claude-att1-entrante-estado-v1` (este documento).
- Instancia: `danschwab2002/setter-instancia-att1`. El PR #13 (merge `d52effc`) puso
  `inbound = true` en el manifiesto; el PR #14 actualiza su README y su camino a producción.
- Antes: `docs/operations/2026-10-01-att1-instancia-y-adaptador-ghl-e2e.md`.

Este documento no contiene teléfonos, nombres de leads, emails, tokens ni el host público del
bridge. Los mensajes de la prueba los mandó Dan desde dos teléfonos propios.

## 1. Qué se prendió

- 2026-10-05 11:18 UTC: Dan mergeó el PR #13 de la instancia (`d52effc`).
- 2026-10-05 11:25 UTC: Dan subió la instancia al VPS, cargó los diez flags del entrante en el
  archivo de secretos del bridge (con respaldo previo), corrió `generar.py` (55 claves, ninguna
  provisional) y redesplegó el stack, con `--force` del bridge.

Los diez flags: `HERMES_SHADOW_ENABLED`, `CHATWOOT_AUTOMATED_REPLIES_ENABLED`,
`CHATWOOT_CUT_B_ADMISSION_ENABLED`, `CHATWOOT_CUT_B_AGENT_ENABLED`,
`CHATWOOT_DURABLE_OPT_OUT_ENABLED`, `CHATWOOT_HUMAN_PAUSE_ENABLED`,
`HUMAN_HANDOFF_ADMISSION_ENABLED`, `HUMAN_HANDOFF_PROJECTION_ENABLED`,
`CHATWOOT_SCOPED_INBOUND_SENDERS_ENABLED` y `PAYMENT_LINK_ENABLED`. Con los tres que estaban en
`true` desde el 2026-10-04 (`COMMERCIAL_KNOWLEDGE_ENABLED`, `GHL_PRECHECKOUT_ADAPTER_ENABLED` y
`PORTABLE_HOTMART_PURCHASE_STOP_ENABLED`) son trece. Los flags quedan en ese archivo y en la spec
del servicio de Swarm; el stack no es de EasyPanel, así que un redeploy del panel no los pisa.

**Corte rápido de las respuestas:** `CHATWOOT_CUT_B_AGENT_ENABLED=false` y redeploy.

## 2. Qué quedó corriendo

Medido el 2026-10-05 a las 12:08 UTC con `docker inspect` y `/ready` desde dentro del contenedor.

| Servicio | Imagen | Contenedor iniciado (UTC) | Health Docker |
|---|---|---|---|
| `setter-att1_att1-bridge` | `setter-bridge:v1.3.1` (`117b344ec277`), compilada en el VPS desde el tag | 2026-10-05 11:25:42 | healthy |
| `setter-att1_att1-db` | `supabase/postgres:17.6.1.167` | 2026-09-30 04:14:42 | healthy |
| `setter-att1_att1-rest` | `supabase/postgrest:v14.15` | 2026-09-30 04:14:40 | sin healthcheck |
| `setter-att1_att1-gateway` | `nginx:alpine` | 2026-10-01 16:35:20 | sin healthcheck |

- El bridge declara `SETTER_VERSION=1.3.1` y `GIT_SHA=100da653e1997d6aea5871ccdf5344e424168634`
  (merge del PR #217). Se comparó el commit, no el artefacto.
- `/ready`: `status: ready`, `instance_product_version: v1.3.1`, `pilot_boundary: disabled`,
  `ghl_precheckout_adapter: enabled:2-forms`,
  `ghl_adapter_risk: accepted:2026-10-04:ghl-precheckout-adapter-v1`,
  `human_handoff_projection: configured`, y en cero las derivaciones pendientes, reintentables,
  en conflicto y en dead letter.
- `att1-db`: 100 migraciones en el registro de la instancia; la última,
  `20261001000500_commercial_case_lookups_by_inbound_kind.sql`.
- En el VPS no queda otra imagen de `setter-bridge` que `v1.3.1`: volver a `v1.3.0` exige compilar
  ese tag.

## 3. El E2E

Desde dos teléfonos de Dan al WhatsApp de ATT1 (inbox 11, cuenta 2 de Chatwoot). Lo de la base se
midió en `att1-db` el 2026-10-05 a las 12:10 UTC, leyendo solo estados, códigos y horas.

| Hora (UTC) | Qué se probó | Resultado |
|---|---|---|
| 11:31 | Primer mensaje | Turno `completed` (`agent_turn_provenance`: perfil `att1-agente-comercial`, bridge `100da65`), **sin respuesta**. Ver §4 |
| 11:37 | Una pregunta sobre el programa | Turno `completed`. Contestó con el conocimiento `v1` aprobado: qué es, el precio en USD y que se compra en Hotmart (visto en el teléfono) |
| 11:41 | Pedir el link | Turno `completed` y una fila en `checkout_link_issuances`: `accepted_by_chatwoot`, `offer_resolution = default_no_intent`, `attribution_resolution = marker_only`, `source_kind = inbound_request`, `sck_format_version = v1`, `sck_value = hermes\|v1\|01M45XXQRE387T4MYWFK2P6DXA`, sin `original_sck`. La URL va a `pay.hotmart.com/D98014973Y` con los parámetros `off`, `checkoutMode`, `src` y `sck` |
| 11:47 | Preguntar por cuotas | Turno `completed` y una fila en `human_handoff_requests`: `projected` a las 11:47:39, `commercial_exception`, team esperado 2, política `att1-derivacion-entrante`, efectos `assignment=applied` y `private_note=applied`. En Chatwoot: team «att1 - revisión humana», etiqueta `automation_paused` y nota interna con el motivo |
| 11:54 | «No más mensajes», desde el otro teléfono | Turno `completed` y una fila en `contact_opt_out_events`: `stop_receiving_messages`, `purpose = cart_recovery`, `correlation_status = applied`, `projection_status = applied`, sin error. En Chatwoot, etiqueta `automation_opted_out`. El agente no volvió a escribir |

El opt-out frena todos los flujos de salida para ese teléfono, no solo el carrito: las consultas
que deciden un envío filtran `contact_opt_out_events` por fuente, canal, cuenta y variantes del
teléfono, sin mirar `purpose`
(`supabase/migrations/20261001000200_portable_precheckout_first_contact.sql:320`,
`supabase/migrations/20261001000100_whatsapp_phone_equivalence.sql:810` y `:1278`).

`recuperador_estado.py --instancia att1` del 2026-10-05 a las 12:05 UTC: 1 link emitido (el de la
prueba, entregado), 7 entrantes en 2 conversaciones en 24 horas, las dos de la prueba. Ningún lead
real le había escrito todavía al inbox 11 desde la activación.

## 4. Lo que faltaba: la asignación automática del inbox

El primer mensaje no tuvo respuesta. El agente armó el turno, pero Chatwoot le había asignado la
conversación a una persona con la asignación automática del inbox 11 («Assigned to … by Default
Policy»), y el bridge no contesta una conversación con una persona asignada:
`src/bridge/chatwoot.py:1032-1036` de `v1.3.1` devuelve `human_assignee_present` sin escribir una
línea en el log. Dan se desasignó la conversación y apagó la asignación automática del inbox 11.
Desde ahí, el E2E pasó entero.

En Johanna pasó lo mismo y se resolvió igual
(`docs/operations/inbound-clinical-handoff-e2e-20260824.md`). Apagarla ahora es un paso del camino
a producción de la instancia (PR #14 de `setter-instancia-att1`).

## 5. Lo que sigue abierto

- Al derivar, el lead no recibe ningún mensaje: el agente se calla y la conversación pasa al team 2.
- En Chatwoot, las acciones del bridge figuran como de Dan, porque el token de control es de su
  usuario.
- Las derivaciones las atiende Mariana (decisión de Dan del 2026-10-04). Todavía no está dada de
  alta en la cuenta 2 de Chatwoot ni le llegan avisos por Slack.
- El primer contacto, el pago fallido y el carrito siguen apagados y sin E2E. Tampoco tiene E2E en
  ATT1 la respuesta a una plantilla (H7).
- La compra aprobada ya marca la intención (webhook de Hotmart desde el 2026-10-04), pero todavía
  no llegó ninguna compra real que confirme el nombre del producto y la forma del teléfono.
