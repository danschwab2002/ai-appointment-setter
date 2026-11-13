---
name: programar-un-envio
description: "Un mensaje a un grupo en una fecha, cada tanto o a mano"
---

# Programar un envío a una audiencia

**Cargala antes de tu primera respuesta si alguien quiere mandarle un mensaje a un grupo de personas en un momento, y no cuando a cada una le pasa algo.** Por ejemplo: «el 1 de diciembre a las 10 mandales el recordatorio de Black a los que dejaron el formulario», «todos los lunes avisales a las clientas», «quiero poder lanzar un aviso cuando yo diga». Si el mensaje sale porque la persona hizo algo (compró, abandonó el carrito, le rechazaron el pago), no es esta guía: es `crear-un-flujo`.

## Lo que no cambia

Las reglas de la charla son las de `crear-un-flujo` (lenguaje de negocio, rondas cortas con un valor propuesto, no preguntar lo que ya dijo, leer con `list_flows` y `list_templates` antes de proponer). Esta guía suma solo lo propio de un envío a un grupo.

## Las tres formas

- **En una fecha.** Una sola vez, un día y una hora, en la zona de la instancia. Proponé la zona de la instancia; preguntá el día y la hora si no los dijo.
- **Cada cierto tiempo.** Ciertos días de la semana a una hora (como mucho una vez por día), y opcionalmente desde y hasta qué día. Proponé un «hasta»: un aviso que se repite sin fin casi nunca es lo que se quiere.
- **A mano.** Lo lanza una persona del equipo cuando quiere, desde la página. **Vos nunca lo lanzás**: armás el flujo, y después una persona arma la lista y la confirma.

## A quién le llega (la audiencia)

- **Prospectas:** quien dejó el formulario y aceptó que le escriban por WhatsApp, en los últimos días que digas (proponé 30). Quien ya compró no entra.
- **Clientas:** quien compró (el producto que digas, o cualquiera de la instancia). A una clienta solo se le escribe con una plantilla de utilidad y con el OK del cliente, como con la compra.
- **Con etiquetas, si hace falta:** «solo las que tienen la etiqueta de Black», «menos las VIP». Son las etiquetas de las conversaciones (la guía `chatwoot-whatsapp`). Si no nombra ninguna, no la propongas.
- Quien pidió no recibir más mensajes **nunca** entra, aunque esté en la lista. Contalo como dado, no lo preguntes.
- La misma persona no recibe otra vez el mismo envío antes de **7 días** (proponelo; si quiere otro número, que lo diga).

## El ritmo y los frenos (contalo siempre en el resumen)

- Sale de a **200 personas por hora** como mucho, y **1.000 por envío** como máximo. Si la audiencia es más grande, decilo: entran los primeros mil. Si quiere menos por hora, se baja; más de 1.000 por hora o de 5.000 por envío no se puede.
- **Se frena solo si baja la salud del número**: si Meta pone el número en amarillo o en rojo, o si no se pudo leer la salud en el último día, el envío se frena antes de mandar y el equipo recibe un aviso en el canal de alertas. Un envío frenado no sale más; si se quiere, se arma otro cuando el número esté sano.
- Una persona del equipo puede frenarlo a mano desde la página en cualquier momento.

## La confirmación (la hace una persona, nunca vos)

- **En una fecha y cada cierto tiempo:** el sí es el de la publicación del flujo (lo aprueba un aprobador en la página, como cualquier flujo). Después sale solo en cada fecha.
- **A mano:** después de publicado, una persona del equipo entra a la página de aprobación, sección **«Envíos a una audiencia»** (la página de aprobación con `/envios` al final), toca «Armar la lista», **ve cuántas personas van a salir** y cuántas quedaron afuera y por qué, escribe ese número y marca «Confirmo». Si el número no coincide, no sale. Mandale el link de la página y contale estos pasos en dos líneas.
- No digas que ya salió, ni cuántas personas lo van a recibir, antes de que la persona lo vea en la página: la lista se arma en ese momento.

## Lo que todavía no se puede

- Si al validar el sistema dice que este disparo **todavía no lo corre**, decilo así, en una frase: el envío queda armado pero no se puede publicar hasta que el sistema nuevo lo ejecute; ofrecé dejarlo listo como propuesta. No lo armes por el sistema anterior.
- Un envío a gente que no dejó el formulario ni compró (por ejemplo, una lista comprada o de otro lado) no se puede: decilo en la primera ronda.
- Varios mensajes seguidos en un envío, o una condición («si no contesta, mandale otro»), siguen las reglas de `crear-un-flujo`.

## El resumen

Antes de armarlo, en una sola ronda: qué quiere lograr, la forma (fecha y hora, días, o a mano), a quién le llega (prospectas o clientas, la ventana, las etiquetas), la plantilla, el ritmo (200 por hora, 1.000 por envío) y los frenos, la repetición (7 días) y, si es a mano, quién lo confirma en la página. Con el OK, armalo como cualquier flujo (`propose_flow_change`, `validate_flow_change`) y mandá el link de aprobación.
