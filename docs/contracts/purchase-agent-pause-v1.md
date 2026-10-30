# Contrato: al comprar, el agente deja de atender (purchase-agent-pause-v1)

Origen: E-100 (2026-10-25), la réplica de «Ventas maribel» de GHL. En GHL, ese
workflow se dispara con la compra: le quita el usuario asignado, le quita la
etiqueta del carrito y apaga el bot.

## Qué hace

Con `PURCHASE_AGENT_PAUSE_ENABLED=true` (apagado por defecto), el worker de
resolución, después de aplicar una compra aprobada
(`apply_hotmart_purchase_approved`) de un producto de la instancia:

1. lee de la base las conversaciones de esa persona con la instancia:
   `read_purchase_agent_pause_conversations_v1(p_chatwoot_account_id,
   p_chatwoot_inbox_id, p_inbound_scope_key, p_buyer_email, p_buyer_phone)`
   (migración `20261025000400`, solo `service_role`, solo lectura);
2. en cada una (hasta 5, la más reciente primero):
   - pone la etiqueta `automation_paused` con el macro de pausa
     (`CHATWOOT_PAUSE_MACRO_ID`), confirmada leyendo;
   - si `[chatwoot].equipo_derivacion` está en el manifiesto y nadie tiene la
     conversación, la asigna a ese team; una persona o el mismo team se dejan
     como están, y otro team es un conflicto que va al log;
   - deja una nota privada: «Compró el DD/MM/AAAA: la atiende el equipo…», con
     el marcador `[supportmagician-compra-pausa:<id del evento>]`, una sola vez
     por compra.

## La lectura de la base

Una conversación entra si es de la cuenta de Chatwoot de la instancia, por
WhatsApp, y es de la instancia (la identidad atada a su inbox, un caso del
entrante de su scope o un caso de recuperación de un scope de su inbox), y
además:

- por teléfono: los últimos 10 dígitos del WhatsApp de la conversación son los
  últimos 10 del teléfono de la compra (cubre 52/521 y 54/549);
- o por mail: el contacto de la conversación tiene ese mail.

Devuelve filas `(outcome, chatwoot_conversation_id, last_activity_at)`:
`found` con las conversaciones; una sola fila `ambiguous` sin conversación si
las encontradas son de más de un número de WhatsApp (las que no tienen número,
como la del carrito, no cuentan); cero filas si no hay ninguna. Argumentos
inválidos: `22023 invalid_purchase_agent_pause_read`.

## Lo que no hace

- No frena la compra: cualquier error (la lectura, Chatwoot, la nota) va al log
  (`purchase_agent_pause_failed`, `purchase_agent_pause_note_not_sent`) y la
  compra queda aplicada. No hay reintento.
- No pausa al comprador que tomó un flujo nativo de compra
  (`reason=native_flow_started`): su plantilla de bienvenida va a la misma
  conversación, y una conversación pausada frena los envíos proactivos. Desde
  E-165 (`flow-engine-v2` 2.8.0, sección 20) la pausa va después de la
  admisión nativa: el comprador que el flujo no tomó (fuera de la cohorte, ya
  disparado, sin flujo o sin poder entrar) se pausa como sin flujo nativo.
  Mientras la admisión no contesta (`reason=native_admission_pending`) espera,
  y la vuelta de la cola que obtiene respuesta pausa si ningún flujo arrancó.
  Hasta la 1.20.5, un flujo nativo en `cohorte` o `prendido` salteaba la pausa
  para todos los compradores del producto (`reason=native_flow_on`).
  Con `PURCHASE_AGENT_PAUSE_AFTER_NATIVE_WELCOME_ENABLED` (abajo), ese
  comprador se pausa igual, después de la bienvenida.
- No pausa compras de un producto que no es de la instancia
  (`reason=other_product`) ni compras sin mail ni teléfono.
- No quita etiquetas de Chatwoot (la del carrito de GHL no existe acá).

## La pausa después de la bienvenida nativa (E-190)

Con un flujo nativo de la compra `prendido`, el flujo toma a todo comprador, y
sin esto ninguno quedaba pausado: el agente le seguía contestando como a un
prospecto (Maribel #27). Con `PURCHASE_AGENT_PAUSE_AFTER_NATIVE_WELCOME_ENABLED=true`
(apagado por defecto), el despachador, apenas Chatwoot acepta el envío de un
paso nativo cuya versión fijada entra con `permiso: compra`, y después del paso
`etiquetar`:

- pausa **la conversación a la que fue la bienvenida** (la que la persona
  contesta), con la misma etiqueta, el mismo team y la misma nota que al
  comprar. No lee la base: la conversación es la del envío;
- el marcador de la nota lleva el id de la acción (una nota por envío) y tiene
  el mismo prefijo, así que la reanudación por silencio la respeta igual;
- deja `purchase_agent_paused_after_welcome action_id=… conversation=…` o, si
  falla, `purchase_agent_pause_after_welcome_failed` (nunca frena: el envío ya
  salió). Un paso de un flujo del carrito (`permiso: formulario`) o de una base
  sin `entry_permission` (anterior a `20261028000200`) no se pausa
  (`purchase_agent_pause_after_welcome_skipped`).

Un flujo nativo tiene un solo envío por camino y ninguna espera después
(`flow-engine-v2`, sección 9): la pausa no frena ningún paso del flujo.

Desde la revisión E-196, la pausa corre en **todo cierre definitivo** del paso
de la compra, no solo en el aceptado:

- entregado sin aceptación confirmada (`acceptance_finalization_failed`): la
  conversación del envío, sin la cola `etiquetar`;
- rechazado por el remitente, sin resultado claro (`delivery_unknown`) o
  cerrado antes del pedido (`permanent_failed`/`expired`, sin más reintentos):
  las conversaciones del comprador, buscadas como al comprar con el teléfono y
  el mail del contexto de la acción. Un fallo que todavía se reintenta no pausa.
  Sin el flujo nativo, ese comprador se habría pausado igual al comprar.

Un error de red, un 5xx o una asignación sin confirmar
(`purchase_pause_assignment_not_confirmed`) se reintentan dos veces (2 s y
5 s). Si sigue fallando, o el error no es transitorio, queda una nota privada en
la conversación que le pide al equipo poner `automation_paused` a mano, con el
marcador `[supportmagician-compra-pausa-pendiente:<action_id>]` (otro prefijo:
la reanudación no la toma por una pausa puesta), y el log
`purchase_agent_pause_after_welcome_pending`. Con otro team ya asignado, la
etiqueta queda puesta y se escribe la nota de la compra igual.

Deuda de E-100, sin tocar: `pause_buyer` (la pausa al comprar) no reintenta y,
con otro team asignado, deja la etiqueta sin la nota, así que la reanudación por
silencio puede levantarla. Exige `PURCHASE_AGENT_PAUSE_ENABLED`, `FLOW_ENGINE_ENABLED` y
`DURABLE_DISPATCHER_ENABLED`; sin ellos, el bridge no arranca con el flag.

## Cómo vuelve al agente

Como hoy: una persona del equipo saca la etiqueta `automation_paused` a mano. La
reanudación por silencio del equipo (`CONVERSATION_RESUME_ENABLED`) no la
devuelve: si la conversación tiene la nota de una compra, se saltea
(`conversation_resume_skipped reason=purchase_pause`).

## Requisitos para arrancar

El manifiesto de la instancia, `RESOLUTION_WORKER_ENABLED`,
`HOTMART_PURCHASE_WORKER_ENABLED` y el cliente de control de Chatwoot con
`CHATWOOT_PAUSE_MACRO_ID`. Sin ellos, el bridge no arranca con el flag prendido.

La evidencia de que funciona va a `docs/operations/` después del E2E.
