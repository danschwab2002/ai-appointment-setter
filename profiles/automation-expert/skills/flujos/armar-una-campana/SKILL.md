---
name: armar-una-campana
description: "Armar una campaña o promo con su cupón y palabra clave"
---

# Armar una campaña

**Cargala antes de tu primera respuesta si alguien quiere una campaña o una promo por tiempo limitado**, con su cupón o su palabra clave. Por ejemplo: «armame una campaña de Black November con este cupón y esta palabra clave», «hagamos una promo del 20 al 30», «¿cómo va la campaña?», «pausá la campaña».

## Primero: ¿están prendidas en esta instancia?

**Tu primera herramienta es `list_campaigns`, antes de preguntar y antes de crear nada.**

- Si vuelve `campaigns_not_enabled`: las campañas no están prendidas en esta instancia. Decilo así («las campañas todavía no están prendidas acá; las prende el equipo») y frená. **No crees la etiqueta ni el macro, no llames a `preview_campaign` y no digas que va a quedar andando.** Podés ofrecer anotar lo que quiere para cuando esté.
- Si vuelve cualquier otro error, tampoco toques Chatwoot: decí que no pudiste comprobarlo y que lo reintentás.
- Solo con una lista (vacía o no) seguí con lo de abajo.

## Lo que esto hace, y lo que no

Dicho en una frase, con las campañas prendidas: **te la armo acá, la aprueba alguien de la lista en una página, y queda andando sin subir nada al servidor.** Entra a la campaña quien escribe con la palabra clave o contesta que sí a la pregunta de entrada; el link de pago le sale con el cupón.

- **No manda nada a una lista**: nadie recibe un mensaje por publicarla. Si quiere avisarle a su base, eso va por fuera.
- **La entrada por una landing propia y el carrito de esa landing** todavía no se arman acá: las configura el equipo.
- **Una sola campaña andando por instancia.** Los precios por país van adentro de la misma campaña. Si ya hay otra andando, decilo antes de armar.
- **No publicás ni pausás vos.** Lo hace una persona de la lista de aprobadores en la página.
- **El control de calidad lo corre el equipo** (compara la campaña con lo que el agente ya sabe; hasta 15 minutos). Decilo sin prometer cuándo: no empieza solo.

## Cómo se conversa

Las reglas de `crear-un-flujo`: lenguaje de negocio, mensajes cortos, una o dos preguntas por mensaje con un valor propuesto, y si ya lo dijo, no lo preguntes.

Con lo que devolvió `list_campaigns` ya sabés si hay una andando o una a medias. Lo que tiene que quedar claro:

1. **Nombre y para qué.** Elegís vos la clave (corta, minúsculas y guiones, por ejemplo `black-november-2026`); se la contás en el resumen.
2. **Cuándo.** Desde y hasta, con el día y la hora y la zona de la instancia. El «hasta» es exclusivo: hasta el 1/12 a las 00:00 es hasta el 30/11 a las 23:59.
3. **La oferta.** El producto si hay varios, el precio y la moneda, el precio de antes, el cupón, qué incluye, garantía, medios de pago. **Los precios por país van acá**, en `se_puede_decir` o en `asesoramiento`, uno por país. Lo que cambia a propósito respecto de lo de siempre va en `reemplaza`.
4. **El cupón.** No se puede comprobar desde acá que exista en Hotmart: pedí que lo confirme.
   - **La oferta propia, si la tiene.** Si la campaña se vende con otra oferta de Hotmart (otro link de pago, por ejemplo uno con un bono), va en `oferta.codigo_oferta`: es el `off=` del link de pago de la página de la campaña (cómo leerlo, en la guía `hotmart`). Tiene que ser una oferta **declarada en la instancia**: si `preview_campaign` vuelve con `campaign_offer_unknown`, decí que primero hay que sumar esa oferta a la instancia (lo hace el equipo, no se puede desde acá) y seguí sin el campo o esperá. Con la oferta declarada, el link de pago que manda el agente en la campaña sale con esa oferta; si en el momento no se puede emitir, la conversación pasa a una persona. Que la oferta esté activa en Hotmart no se puede comprobar desde acá (`campaign_offer_not_verified`): pedí que lo confirme. Si la instancia tiene prendida la entrada por oferta, quien deja un carrito o un pago rechazado de esa oferta también entra a la campaña (le llega la etiqueta antes del primer mensaje): contalo así. Si `preview_campaign` vuelve con `campaign_offer_entry_taken`, esa oferta ya es de otra campaña de la instancia: decilo y no la uses.
5. **Cómo entran.** Las palabras con las que escriben (el botón de la landing las lleva escritas) y, si quiere, la pregunta para los que escriben por su cuenta («¿Nos escribes por la promoción de Black November?»). Tiene que haber al menos una de las dos.
6. **Cómo tiene que hablar el agente.** A quién recomendarle qué, qué sí se puede decir, qué no está confirmado (lo deriva) y qué promesas no se hacen.

No inventes precios, fechas, bonos ni garantías. Lo que falta, se pregunta; lo que no se sabe, va en `no_confirmado`.

## Los pasos

1. **Etiqueta y macro**, solo después de que `list_campaigns` contestó. Con `create_chatwoot_label` creá la etiqueta (minúsculas, sin espacios, por ejemplo `black-november`) y con `create_chatwoot_label_macro` el macro que solo suma esa etiqueta: su `id` es `macro_etiqueta`. Si vuelve `chatwoot_macro_rejected` o `chatwoot_tool_not_enabled`, el macro lo crea el equipo: decilo y pedí el número.
2. **La vista previa.** `preview_campaign` valida y muestra cómo la va a leer el agente, sin guardar nada. Si vuelve con `blocks`, corregí vos y volvé a probar (`field` y `problem` dicen qué). Mostrá lo importante en palabras, no el bloque crudo.
3. **El resumen.** Una línea por cosa, igual que en `crear-un-flujo`, y **esperá el sí**. Sin ese sí no se guarda.
4. **Guardar.** `propose_campaign`, con `purpose` en una línea. Guardá el `campaign_id` y la `revision` que devuelve.
5. **Validar.** `validate_campaign`. Si vuelve `blocks`, corregí con `propose_campaign` (mandando `campaign_id` y `revision`) y validá de nuevo. Con el macro que hace de más o que no se puede leer, no se corrige escribiendo: hay que hacer otro.
6. **Contá cómo sigue.** «Ya quedó armada. Falta el control de calidad, que lo corre el equipo; cuando pase, alguien de la lista la aprueba en la página y queda andando. Nadie la recibe antes.» No digas que quedó publicada.
7. **El link para aprobar.** `validate_campaign` y `get_campaign` traen `approval_url`: mandáselo a la persona, así, sin explicar qué es un id. La página pide entrar con Slack; solo entran los de la lista.

## Después

- **Si preguntan cómo va**, `get_campaign`: el estado, y en `check_report` lo que dio el control de calidad.
- **Si el control dejó preguntas** (`check_report.veredicto` = `abiertos`) y `check_report` **no trae `criterio`**: pasáselas a quien te pidió la campaña, una por mensaje. Con las respuestas, sumalas en `preguntas_del_alta` (`pregunta`, `respuesta`, `fuente`: quién y cuándo) y, si cambia un dato, cambialo también donde esté. Rehacela con `propose_campaign` y validá de nuevo: vuelve al control de calidad.
- **Si `check_report` trae `criterio`**: `items` es lo que **frena la publicación** (un precio, un cupón, una garantía o una promesa que chocan con algo escrito, en `razon`, y las dos partes que chocan en `citas`) y `avisos` es lo que no frena.
  - Con `abiertos`: contale qué choca, **hasta 3 temas por mensaje**, juntando lo que sea lo mismo, con las palabras de su campaña. No digas «segmento», «reemplaza», «base», «campo» ni el número de item. Lo que no frena, en una sola línea al final. Con las respuestas, seguís igual: `preguntas_del_alta`, `propose_campaign` y validar.
  - Con `limpio`: el control no encontró nada que frene. **No digas que ya se puede aprobar como certeza**: desde acá no podés comprobar si sigue vigente (si el conocimiento general del agente cambió después, la página no muestra el botón). Mandá el link y decí que la página confirma si se puede aprobar. Si trae `avisos`, contá en una línea qué quedó para mirar, que está en la página y que no hace falta corregirlo para publicar. Si quiere mejorarlo, se rehace y vuelve al control.
- **Si `check_report.veredicto` = `sin_veredicto`**: el control no llegó a un resultado. Decilo así, sin inventar preguntas ni decir que pasó: hay que volver a correrlo, lo corre el equipo, y mientras tanto no se puede aprobar.
- **Una campaña aprobada no se edita.** Para corregirla: un aprobador la pausa en la página, armás la versión siguiente con la misma clave, y pasa otra vez por todo.
- **Pausar y reanudar** los hace un aprobador en la misma página, sin subir nada. Pausar no le saca la etiqueta a nadie: el agente deja de usarla enseguida.
- **Si te dicen que «no la está usando»**, puede que el conocimiento general del agente haya cambiado después del control de calidad: hay que volver a correrlo. Decilo y que lo corra el equipo.
