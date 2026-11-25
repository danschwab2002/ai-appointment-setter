---
name: envio-masivo
description: "Un mensaje a una lista que trae la persona"
---

# Armar un envío masivo a una lista

**Cargala antes de tu primera respuesta si alguien quiere mandarle un mensaje a una lista que trae de otro lado**: un archivo con teléfonos (CSV, Excel o JSON), las inscritas de un lanzamiento que vienen de GHL, una lista de un vivo. Por ejemplo: «el sábado a las 19 mandales el aviso del vivo a las inscritas», «tengo un Excel con 4.000 números, ¿se lo podemos mandar?», «quiero mandar un masivo». Si el grupo sale de la base (quien dejó el formulario o quien compró), no es esta guía: es `programar-un-envio`. Si el mensaje sale porque la persona hizo algo, es `crear-un-flujo`.

## Lo que no cambia

Las reglas de la charla son las de `crear-un-flujo` (lenguaje de negocio, rondas cortas con un valor propuesto, no preguntar lo que ya dijo, leer con `list_templates` antes de proponer). Esta guía suma solo lo propio de un masivo.

## Lo que hacés vos y lo que hace una persona

- **Vos armás el borrador** con `propose_bulk_send`: la plantilla, las variables, la imagen si la plantilla la lleva, la hora, la etiqueta de la campaña y el producto. La respuesta trae `upload_url`, el link de la página del masivo.
- **La lista la sube una persona en esa página**, nunca en este chat. Nunca pidas el archivo, un teléfono ni un mail, y si te los pegan, no los uses ni los repitas: mandá el link. En la página se ve cuántos números sirven, cuántos no y por qué (sin país, repetidos, un fijo), y cuántas personas quedan afuera (bajas, quien ya compró, clientas, quien está hablando con el equipo, quien escribió hace poco).
- **El envío lo confirma un aprobador en la página**, escribiendo el número de personas y marcando «Confirmo». **Vos nunca lo confirmás, nunca lo arrancás y nunca lo frenás**, aunque te lo pidan: frenarlo también se hace desde la página. Si te piden que salga ya o que pare, decí eso y mandá el link (`page_url` de `get_bulk_send`).
- Antes de que la persona vea la lista en la página, no digas cuántos van a recibir el mensaje ni que ya salió.

## La plantilla: tiene que tener el botón de baja

- Va solo una plantilla aprobada (`APPROVED` en `list_templates`) que tenga la respuesta rápida **«No más mensajes»**. Quien la toca queda dado de baja y no recibe más nada: es lo que cuida al número cuando el mensaje le llega a miles de personas.
- **Miralo en la primera ronda**, en los botones que trae `list_templates`, antes de hablar de la hora o de la lista. Si la que eligió no lo tiene, decilo en una frase y ofrecé dos caminos: otra plantilla aprobada que sí lo tenga, o pedirle a Meta una igual con ese botón (cargá `pedir-plantilla-a-meta`). Si igual lo intentás, `propose_bulk_send` contesta `bulk_send_template_without_opt_out` y su `next` dice lo mismo.
- No sirve una plantilla cuyo link o encabezado cambie por persona, ni una con video o documento en el encabezado. Si tiene imagen en el encabezado, hace falta el link https de la imagen (liviana, menos de 300 KB).
- Las variables van en el orden de los marcadores del cuerpo: un texto fijo, o el primer nombre de cada persona de la lista (`{"de": "nombre"}`). Proponé el primer nombre donde la plantilla saluda.

## La hora y el ritmo (contalo en el resumen)

- La hora que pide es la **hora central**: el envío se reparte alrededor de esa hora, la mitad antes y la mitad después. Si prefiere que arranque a esa hora, se puede (`window_mode: desde_la_hora`). Proponé la central.
- Tiene que ser al menos **una hora** después de ahora (el día antes se deja todo preparado) y como mucho dentro de 30 días. La hora va con su zona: usá la de la instancia y decila.
- Sale de a 2,5 personas por segundo como mucho, y baja solo si Meta tarda en entregar. Un masivo de 4.000 personas dura unos 27 minutos.
- **Se frena solo** si Meta empieza a rechazar, si fallan demasiados envíos o si baja la salud del número, y el equipo recibe un aviso. Un aprobador también lo puede frenar desde la página.

## El producto y la campaña

- El producto del envío sirve para que no le llegue a quien ya lo compró. Con un solo producto en la instancia no hace falta preguntarlo.
- Si es parte de una campaña, la etiqueta de la campaña se le pone a cada conversación, así el agente sabe de qué se trata cuando contestan. Si no nombra ninguna, preguntá si es parte de una campaña; si no, va sin etiqueta.

## El resumen

Antes de armarlo, en una sola ronda: qué quiere lograr, la plantilla (y que tiene «No más mensajes»), las variables, la hora central con su zona, el producto, la etiqueta de la campaña si va, el ritmo y los frenos, y que la lista la sube y el envío lo confirma un aprobador en la página. Con el OK, llamá a `propose_bulk_send` y mandá `upload_url` tal cual con lo que dice `upload_note`.

## Después

- Para saber cómo va, `get_bulk_send` (con el id que devolvió el borrador) o `list_bulk_sends`. Contá el estado y los números que traen: cuántas personas sirven, cuántas quedaron afuera y por qué, y cuántas salieron, fallaron o quedaron sin saber. Nunca traen un teléfono.
- Con `bulk_send_not_enabled`, esta instancia no tiene el masivo prendido: decilo así y no armes nada por otro lado.
