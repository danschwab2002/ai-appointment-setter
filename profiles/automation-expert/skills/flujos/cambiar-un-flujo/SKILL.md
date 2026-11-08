---
name: cambiar-un-flujo
description: "Cambiar, prender, apagar o volver atrás un flujo que existe"
---

# Cambiar un flujo que ya existe

Cargala cuando te piden cambiar un flujo: una espera, una plantilla, prenderlo, apagarlo o volver a una versión anterior. Por ejemplo: «cambiá la espera del carrito a 2 horas», «poné otra plantilla en la bienvenida», «que la bienvenida le llegue a todos», «volvé a la versión de ayer».

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
- **Lo que una versión no cambia.** En una versión cambiás las esperas, la plantilla (por otra aprobada), el vencimiento, los topes por día y total, y el horario. Otra cosa (los pasos que no son esperas, la prioridad, las salidas, la repetición, el permiso, la bolsa) vuelve con `change_outside_1b_scope`.
- **Reemplazar un flujo del sistema nuevo.** Lo que una versión no cambia, en un flujo del sistema nuevo hoy se hace reemplazándolo, y dos flujos nuevos del mismo producto con el mismo evento no pueden estar prendidos ni en el grupo de prueba a la vez (`same_event_native_flows`, ya en el borrador). En este orden: si ese flujo del sistema anterior está pausado, el operador lo vuelve a armar; un aprobador pausa el viejo desde la página; un `nuevo` con otra clave y lo cambiado (la forma de un `nuevo` está en la guía `crear-un-flujo`), solo en su propuesta, que arranca en su grupo de prueba y con otra bolsa (el tope arranca en cero); comparado, una propuesta que prende el nuevo y apaga el viejo; y el operador vuelve a pausar el del sistema anterior. La que apaga uno y crea o prende otro no se simula: la publica el operador, no la página. Al proponerla, decí en simple que nadie toque Pausar de ese flujo hasta que se publique: si lo pausan, la propuesta queda vieja y el operador la rechaza. Mientras tanto lo manda el sistema anterior; si ese flujo no tiene versión en el sistema anterior, no manda nada: decilo antes de armarlo.
- **Un cambio que no podés escribir** con el formato del flujo se explica igual: qué habría que cambiar y dónde vive ese valor hoy (la base, la configuración de la instancia, una variable o el código).
