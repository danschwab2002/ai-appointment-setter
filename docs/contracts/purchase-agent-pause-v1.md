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
- No pausa si hay un flujo nativo prendido para ese producto
  (`reason=native_flow_on`): su plantilla de bienvenida va a la misma
  conversación, y una conversación pausada frena los envíos proactivos.
- No pausa compras de un producto que no es de la instancia
  (`reason=other_product`) ni compras sin mail ni teléfono.
- No quita etiquetas de Chatwoot (la del carrito de GHL no existe acá).

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
