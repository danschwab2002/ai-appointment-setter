---
name: pausar-el-agente
description: "Que el agente deje de contestar (todas, etiqueta, producto)"
---

# Pausar al agente comercial

**Cargala antes de tu primera respuesta si alguien quiere que el agente deje de contestar**: para todas las conversaciones, para las que tienen una etiqueta o para las de un producto. Por ejemplo: «apagá el bot mientras dura el vivo», «que el agente no conteste a las de Black November», «pausá el agente de El Protocolo hasta mañana», «¿está pausado el agente?», «volvé a prenderlo».

## Qué hace y qué no

- **Frena solo al agente que contesta en el chat.** Mientras la pausa está viva, cuando una persona escribe el agente no corre y no sale nada: ni respuesta, ni aviso. La conversación queda en Chatwoot sin contestar, para el equipo. Una baja («No más mensajes») igual queda registrada.
- **No toca los flujos** (el carrito, la bienvenida, el pago rechazado, un envío programado, un masivo): esos se pausan en la página de aprobación (el link de `get_approval_page`). Si te piden «apagá todo», decí las dos cosas: el agente lo pausás vos, los flujos un aprobador en la página.
- **Al reanudar no se contesta para atrás.** El agente contesta el próximo mensaje de cada persona; lo que entró durante la pausa lo tiene el equipo. `estado_pausa` dice cuántas conversaciones quedaron retenidas: decilo al reanudar para que el equipo las mire.

## Quién puede pedirla

Solo un aprobador (los mismos que entran a la página). Lo decide el sistema por tu Slack: si contesta `not_an_approver`, decí que la pausa la pide un aprobador y no insistas. No hace falta un sí a un resumen ni un botón: frenar no manda nada, así que la ponés en cuanto el aprobador te lo pide.

## Los pasos

1. **Para quién.** Si no lo dijo, preguntá una sola vez: todas, una etiqueta o un producto. En la duda, todas.
   - **Etiqueta**: la etiqueta de Chatwoot tal cual (`list_chatwoot_labels` si no la sabe). Con `chatwoot_label_not_found`, no existe: mostrá las que hay. Con el aviso `agent_pause_label_not_verified`, no pude verificarla: repetí la etiqueta para que la persona confirme que está bien escrita.
   - **Producto**: la clave del producto. `estado_pausa` trae en `productos` los que se pueden pausar solos. Con `agent_pause_product_without_label`, ese producto no tiene etiqueta en Chatwoot: se pausa con «todas».
2. **Hasta cuándo.** Opcional. Si dijo una hora («hasta las 21», «hasta mañana a las 9»), pasala en `hasta` con la zona de la instancia; entre 5 minutos y 7 días. Si no dijo, va sin `hasta` y queda hasta que alguien la saque: decilo en la respuesta.
3. **El motivo**, en una frase, con las palabras de quien pide («vivo de las 19»). Sin teléfonos ni mails.
4. `pausar_agente`. Contestá en una o dos líneas: para quién quedó pausado, hasta cuándo y que los flujos siguen. Con `replayed: true`, ya estaba: decilo y da desde cuándo.

Para reanudar: `estado_pausa` → el `pause_id` de la viva → `reanudar_agente` (con el motivo si lo dijo). Contestá que vuelve a contestar desde el próximo mensaje y cuántas conversaciones quedaron para el equipo.

## Lo que no hacés

- No pongas ni saques `automation_paused` en las conversaciones para pausar al agente: la pausa es una sola llave.
- No digas que el agente está pausado sin que `pausar_agente` haya contestado bien o `estado_pausa` lo muestre.
- Con `agent_pause_not_enabled`, esta instancia no lo tiene prendido: decí que lo pide el equipo técnico.
