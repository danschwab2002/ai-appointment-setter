# Contrato: pausar al agente para todas, por etiqueta o por producto (v1)

- **Estado:** escrito al construir el item 5.4, el 2026-11-26, antes de cualquier merge o despliegue. Describe la interfaz propuesta; no dice que nada esté desplegado ni activado.
- **Contrato:** `agent-global-pause-v1`, versión `1.0.0`.
- **Alcance:** item 5.4 de `documentacion/2026-11-20-lista-definitiva-propuesta.md` («Apagar el bot desde el panel: un vivo, un corte») y D4 y D10 del mapa Z1 del 2026-11-26 (fuera del repo).
- **Migración:** `20261126100000_agent_global_pause_v1.sql`.
- **Código:** `src/bridge/agent_global_pause.py` (la lectura viva y las rutas), el punto del turno en `src/bridge/app.py` y dos lecturas de `service_role` en `src/bridge/supabase.py`. La herramienta del agente: plugin `automation-expert-change` 0.10.0 y la skill `flujos/pausar-el-agente`.
- **Flag:** `AGENT_GLOBAL_PAUSE_ENABLED`, apagado por defecto. Apagado, las rutas no existen (404), el turno no lee la base y `/ready` no cambia. Prendido exige `AUTOMATION_EXPERT_CHANGE_ENABLED`, `FLOW_APPROVAL_ENABLED` (la lista de aprobadores), el manifiesto y `SUPABASE_SERVICE_ROLE_KEY`.

## Qué frena y qué no

Con una pausa viva que alcanza a la conversación, el turno del entrante no llama al agente y no sale nada: ni respuesta, ni aviso de derivación, ni la confirmación de una baja. Es el efecto de `automation_paused` en una conversación, puesto en una sola llave. Una baja («No más mensajes») igual se anota. La admisión del caso y la etiqueta de producto corren como siempre (ninguna manda un mensaje).

No frena los flujos (tienen su pausa en la página, item 5.2), el seguimiento, la reactivación, el masivo ni el comando `reset` de los números de prueba.

## El alcance

| `alcance` | `valor` | Alcanza a |
|---|---|---|
| `todas` | (no va) | toda conversación del inbox |
| `etiqueta` | la etiqueta de Chatwoot | las que la traen en `conversation.labels` del webhook |
| `producto` | la clave del producto (`[[productos]].clave`) | las que traen su etiqueta de producto, o las que el bridge le asigna a ese producto en el mismo turno |

Un producto sin `etiqueta` en el manifiesto no se pausa solo (`agent_pause_product_without_label`). Una etiqueta reservada (`automation_paused`, `automation_opted_out`) no es un alcance. Con la herramienta de Chatwoot del AE prendida, una etiqueta que no existe en la cuenta se rechaza (`chatwoot_label_not_found`); sin ella se acepta por la forma y la respuesta lo avisa (`agent_pause_label_not_verified`).

## Lo que entra durante la pausa

La conversación queda abierta en Chatwoot con el mensaje sin contestar: la ve el equipo como cualquier mensaje sin respuesta. No se le pone `automation_paused`. El bridge anota cada conversación retenida (`agent_global_pause_holds`, una fila por pausa y conversación, con los mensajes contados) y `estado_pausa` devuelve los conteos. **Al reanudar no se contesta para atrás:** nada sale al sacar la pausa; el agente contesta el próximo mensaje de cada persona. Un mensaje escrito mientras la pausa estaba puesta tampoco se contesta después, aunque vuelva al turno (el monitor de conversaciones trabadas lo vuelve a admitir y, sin la etiqueta, el agente lo contestaría): el turno compara el `created_at` del webhook con la ventana de cada pausa de las últimas 48 horas.

## Quién la pone

El AE, directo y sin pasar por la página, porque frenar no manda nada (D4). El plugin manda el ID de Slack de la sesión (`requested_by.slack_user`, nunca el modelo) y el bridge lo compara con `FLOW_APPROVAL_SLACK_APPROVERS`: fuera de la lista, `403 not_an_approver`. La base guarda quién la pidió, el motivo, quién la sacó, por qué y cuándo; la fila no se borra ni cambia salvo levantarla una vez.

## `hasta`

Opcional. ISO 8601 con zona, entre 5 minutos y 7 días desde ahora. Sin `hasta`, la pausa sigue hasta que alguien la saque. Con `hasta`, el turno la deja de aplicar a esa hora con el reloj del bridge, la lectura de la base ya no la devuelve y la próxima escritura la marca levantada por `vencimiento`.

## Las rutas (prefijo `/internal/automation-expert/v1`, clave del plugin de cambios)

- `POST /agent-pause` — `{alcance, valor?, hasta?, motivo, requested_by}` → `201` con `data.pause`, `data.replayed` (la misma pausa ya estaba viva: se devuelve sin cambiarla), `data.aplicada_en_este_bridge` y `data.warnings`.
- `POST /agent-pause/{pause_id}/resume` — `{motivo?, requested_by}` → `200` con `data.pause` y `data.replayed` (ya estaba levantada).
- `GET /agent-pause` — `data.pausado`, `data.vivas`, `data.recientes`, `data.productos` (los que se pueden pausar solos) y `data.este_bridge` (la línea de `/ready`).

La vista de una pausa: `pause_id`, `alcance`, `valor`, `etiqueta`, `estado` (`activa`, `vencida`, `levantada`), `desde`, `hasta`, `motivo`, `pedida_por`, `levantada_at`, `levantada_por`, `motivo_al_levantar`, `conversaciones_retenidas`, `mensajes_retenidos`. Sin teléfonos ni nombres.

Errores: `422` (`invalid_body`, `identifier_not_allowed`, `agent_pause_until_invalid`, `agent_pause_product_unknown`, `agent_pause_product_without_label`, `chatwoot_label_reserved`, `chatwoot_label_not_found`, `invalid_pause_id`), `403 not_an_approver`, `404 agent_pause_not_found`, `409 agent_pause_open_limit` (20 vivas), `503` (la base o Chatwoot no contestan).

## La base

- Del AE (`automation_expert_change`): `pause_agent_globally_v1`, `resume_agent_globally_v1`, `list_agent_global_pauses_v1`.
- Del turno (`service_role`): `get_live_agent_global_pauses_v1` (las vivas y las que terminaron en las últimas 48 horas, con `created_at` y `ends_at`, sin quién ni por qué) y `record_agent_global_pause_hold_v1` (`recorded`, `repeated` o `not_active`).
- Una sola pausa viva por alcance y valor (índice único parcial). Las tablas, sin permisos para la API y con RLS.

## La lectura en el bridge

`LivePauses` lee la base al arrancar y cada 10 segundos, y en el acto después de una ruta de pausa. Si la base no contesta se queda con lo último bueno; sin ninguna lectura buena no hay pausa conocida (la admisión del turno también necesita la base). Una lectura sin la forma esperada no pisa lo último bueno. Con varias pausas que alcanzan a una conversación, se anota la más ancha (`todas`, después `producto`, después `etiqueta`).

`/ready` (solo con el flag): `agent_global_pause` = `none,refresh=<estado>` o `paused:<n>:<alcances>,refresh=<estado>`, con `<estado>` en `ok`, `failing:<n>` o `never_read`. No responde 503: pausar no es una falla.

Log: `agent_global_pause_held pause=<id> scope=<alcance> conversation=<número>` por cada mensaje retenido; `agent_global_pause_live` / `agent_global_pause_gone` al cambiar lo vivo; `agent_global_pause_refresh_failed` y `agent_global_pause_hold_not_recorded` como avisos.
