---
name: cambiar-un-flujo
description: "Cambiar, prender, apagar o volver atrás un flujo que existe"
---

# Cambiar un flujo que ya existe

Cargala cuando te piden cambiar un flujo: una espera, una plantilla, un mensaje más o uno menos, cada cuánto se repite, qué otro flujo espera o corta, prenderlo, apagarlo o volver a una versión anterior. Por ejemplo: «cambiá la espera del carrito a 2 horas», «poné otra plantilla en la bienvenida», «que el pago rechazado no le vuelva a llegar a la misma persona», «que el primer contacto no arranque mientras todavía no salió el mensaje del carrito», «que la bienvenida le llegue a todos», «volvé a la versión de ayer».

A la persona le hablás en lenguaje de negocio: los campos y las claves de esta guía son para vos.

## Los pasos

1. Leé el flujo con `get_flow_definitions`. Cada flujo trae `draft_item` y `draft_base`: copialos tal cual, son el ítem y la `base` que pide `propose_flow_change`.
2. Cambiá solo lo pedido adentro de `draft_item.definicion.pasos` y guardalo con `propose_flow_change`, con `draft_item` en `items`, `draft_base` en `base` y el para qué en una línea, como lo pidió la persona.
3. **Antes de contar el cambio, compará las horas.** La respuesta trae `toques`: cuándo sale cada mensaje, en horas desde el primero, `antes` y `despues`. Una espera mueve todos los mensajes que vienen después. Si la persona dijo una hora para un mensaje (por ejemplo «el último sigue a las 72 horas»), fijate que `despues` diga esa hora; si no, corregí la espera y volvé a proponer.
4. Si vuelve con bloqueos (`flow_change_draft_blocked`), explicá cada uno en simple, corregilo y volvé a proponer con el `proposal_id` y la `revision`. Si el bloqueo dice que no se arregla cambiando la propuesta, decíselo al dueño con su motivo y no vuelvas a proponer. Si vuelve con `reason`, dice qué campo, qué ítem y qué claves faltan (`missing`) o sobran (`extra`): corregí eso y nada más.
5. Validalo con `validate_flow_change` y contá la vista previa: qué cambia, el mensaje como lo ve el lead (`mensajes`, con su `como_lo_ve`), a qué se vuelve si sale mal y lo que todavía no se sabe (`huecos`), con sus palabras.
6. Contá cómo sigue (ver «Quién aprueba, publica y pausa» en tu SOUL).

## Lo que hay que tener en cuenta

- **Un `prender` no trae el mensaje en la vista previa.** Antes de proponer que un flujo le llegue a todos, leé la plantilla de su definición activa (`get_flow_definitions`) y su texto en `list_templates`, y mostrá el mensaje con sus variables: quien aprueba tiene que saber qué va a recibir cada persona.
- **Un flujo nuevo pausado vuelve al grupo de prueba** con un ítem `prender` que lleva `a: "cohorte"` (`{producto, flujo, flow_id, tipo: "prender", a: "cohorte"}`). Sin `a`, `prender` lo pasa a todos. Solo sirve desde `pausado` y en un flujo del sistema nuevo (`native: true`).
- **Qué se cambia con una versión.** En cualquier flujo, las esperas, la plantilla (por otra aprobada), el vencimiento, los topes por día y total, y el horario. En un flujo del sistema nuevo (`native: true`), además:

  | Pedido | Carrito, pago rechazado, primer contacto, bienvenida | Seguimiento con cupón |
  |---|---|---|
  | Cada cuánto se le vuelve a mandar a la misma persona (`topes.repeticion`) | Sí: una vez por persona o por persona y oferta, y cada cuánto, o nunca | No: es uno por conversación |
  | Que no arranque mientras la persona está en otro flujo (`prioridad.no_arranca_mientras`, con la clave de otro flujo del mismo producto que corra en el sistema nuevo; el seguimiento con cupón no se puede esperar) | Sí | No |
  | Más mensajes o menos (hasta tres, cada uno con su espera; con más de uno, `contesta` en `termina.si`, que se suma o se saca). El botón solo en el primero, salvo el cupón propio | Carrito, pago rechazado y primer contacto; la bienvenida, uno | No |
  | El cupón propio de cada persona (`boton: {link: checkout, cupon: personal}`, hasta dos por camino, con `precio` y `vence`, y `cuando` solo en el primero) | Carrito y pago rechazado sin variantes | No |
  | Que corte a otro flujo cuando arranca (`prioridad.le_gana_a`, con la clave de otro flujo del mismo producto que corra en el sistema nuevo; la regla de ATT1: el pago rechazado corta el carrito): a la misma persona se le cancela lo que el otro flujo tenía esperando, salvo un mensaje que ya está saliendo. Con otro flujo (uno del sistema anterior, como el carrito de hoy), `native_change_priority_wins_ref` | Carrito, pago rechazado y bienvenida; el primer contacto no | No |
  | Las variantes del pago rechazado (`pasos`, la cadena de condiciones al final; cada variante empieza con su mensaje y puede seguir con hasta dos más sin botón, con la misma espera en todas y la salida de que conteste. La variante se elige una sola vez, antes del primer mensaje; sin el cupón propio adentro) | Solo el pago rechazado | No |
  | Marcar a quien compró, o poner la etiqueta del segmento (`etiquetar`) | Con la compra, `cliente`; con cualquier evento, la de un segmento | No |
  | El nombre del flujo | Sí | Sí |

  Los mensajes siguientes, el cupón propio y que un flujo corte a otro salen solo si la cuenta ya los manda: si no, la validación vuelve con `native_multitouch_not_running` o `native_personal_coupon_not_running` (o con el motivo de `le_gana_a`). No se arregla con la propuesta: repetí al dueño el motivo y la alternativa del bloqueo, sin inventar cuándo se prende, y ofrecé lo que sí sale (un solo mensaje, sin el cupón propio, o que espere en vez de cortar).

  Después de cada cambio, compará las horas como siempre. Una versión con un cambio de estas filas pide a quien aprueba un segundo paso en la página, aunque el flujo esté en el grupo de prueba: decilo al contar cómo sigue. Si cambia la repetición, decí también que la regla nueva cuenta para quienes ya lo recibieron. Si un flujo va a cortar a otro, decí que corta solo lo que todavía no salió y solo a quien arranca este flujo; un flujo del sistema anterior no se corta. Si un flujo va a esperar a otro, decí hasta dónde llega: frena solo mientras el otro flujo tiene a la persona esperando su mensaje. Un flujo de un solo mensaje termina apenas lo manda, así que, si no tiene una espera antes, casi nunca va a frenar nada.
- **A quien va por la mitad** (le salió el primer mensaje y espera el segundo) lo sigue la versión que lo empezó (`en_curso: seguir`, lo que trae `draft_item`). **Si la versión vieja tenía un error** (una plantilla equivocada, una espera mal puesta), proponé `en_curso: cancelar`: a esas personas no les sale ningún mensaje más de ese flujo (tampoco pasan a la versión nueva), y un mensaje que ya está saliendo no se toca. Decilo así y que pide a quien aprueba un segundo paso en la página. Solo en un flujo del sistema nuevo: en uno del sistema anterior vuelve con `en_curso_not_supported`. Pasarlos a la versión nueva (`migrar`) no existe.
- **Lo que no se cambia, y qué decir.** Si te piden otra cosa (otra salida, quién entra, el cupo), proponelo igual: el bloqueo trae el motivo y la alternativa en palabras del dueño. Sacá ese cambio, y decile el motivo con sus palabras. **No hace falta el equipo técnico** para lo de la tabla, y lo del bloqueo tampoco se arregla con el equipo técnico: es lo que el sistema nuevo todavía no hace.
- **Un flujo del sistema anterior** (`native: false`): su estructura vuelve con `change_outside_1b_scope`; la cambia el equipo técnico. Con el flujo en el sistema nuevo, se hace con una versión.
- **Otro disparador u otra bolsa** son otro flujo: reemplazarlo. Dos flujos nuevos del mismo producto con el mismo evento no pueden estar prendidos ni en el grupo de prueba a la vez (`same_event_native_flows`, ya en el borrador). En este orden: si ese flujo del sistema anterior está pausado, el equipo técnico lo vuelve a armar; un aprobador pausa el viejo desde la página; un `nuevo` con otra clave y lo cambiado (la forma de un `nuevo` está en la guía `crear-un-flujo`), solo en su propuesta, que arranca en su grupo de prueba y con otra bolsa (el tope arranca en cero); comparado, una propuesta que prende el nuevo y apaga el viejo; y el equipo técnico vuelve a pausar el del sistema anterior. La que apaga uno y crea o prende otro no se simula: la publica el equipo técnico, no la página. Al proponerla, decí en simple que nadie toque Pausar de ese flujo hasta que se publique: si lo pausan, la propuesta queda vieja y el equipo técnico la rechaza. Mientras tanto lo manda el sistema anterior; si ese flujo no tiene versión en el sistema anterior, no manda nada: decilo antes de armarlo.
- **Un cambio que no podés escribir** con el formato del flujo se explica igual: qué habría que cambiar y dónde vive ese valor hoy (la base, la configuración de la instancia, una variable o el código).
