---
name: link-con-palabra-clave
description: "El link wa.me de un segmento que pone una etiqueta"
---

# Un link de WhatsApp que pone una etiqueta

**Cargala antes de tu primera respuesta si alguien quiere un link de WhatsApp por segmento** (soporte, check-in, asistencia, alumnos) para saber de dónde viene quien escribe. Por ejemplo: «quiero un link para soporte que les ponga la etiqueta soporte», «el botón de la página de alumnas, que las marque», «¿qué links tenemos andando?». Si es una campaña con cupón y conocimiento propio, no es esta guía: es `armar-una-campana`.

## Primero: ¿están prendidas en esta instancia?

**Tu primera herramienta es `list_keyword_rules`, antes de preguntar y antes de crear nada.**

- Si vuelve `keyword_rules_not_enabled`: no están prendidas en esta instancia. Decilo así («los links con palabra clave todavía no están prendidos acá; los prende el equipo») y frená. **No crees la etiqueta ni el macro.**
- Con cualquier otro error, tampoco toques Chatwoot: decí que no pudiste comprobarlo.

## Lo que hace, y lo que no

Dicho en una frase: **si el primer mensaje de una conversación trae el texto del link, la conversación recibe la etiqueta, y el agente sabe que es el texto de un link y no un pedido de hablar con alguien.**

- **No manda ningún mensaje y no arranca ningún flujo** por sí sola. Si quiere que además le llegue algo, eso es otro pedido (`crear-un-flujo`): un flujo nuevo con `dispara: {palabra_clave: <clave de la regla>}`, `entra: {permiso: conversacion}` y hasta tres plantillas. Solo se puede publicar si la instancia tiene prendido el flujo de la palabra clave (si la validación dice `motor_nativo:disparador`, no lo tiene) y la regla ya está andando (si dice `keyword_rule_unknown`, falta aprobar la regla).
- **Solo el primer mensaje** de una conversación que empieza la persona. Si responde a un mensaje nuestro con esa palabra, no cuenta.
- **La campaña gana**: si el texto choca con la palabra clave de una campaña, no se puede guardar.
- **No publicás, ni pausás, ni descartás vos.** Lo hace una persona de la lista de aprobadores en la página.

## Los pasos

1. Proponé el texto del link (corto, el que la persona no va a cambiar: «Hola, necesito soporte») y la etiqueta (minúsculas y guiones). Una o dos preguntas por mensaje, con un valor propuesto.
2. Creá la etiqueta y el macro que solo la suma: `create_chatwoot_label` y `create_chatwoot_label_macro`. El id del macro va en `macro_etiqueta`.
3. `preview_keyword_rule` con `clave`, `nombre`, `palabra_clave`, `etiqueta` y `macro_etiqueta`. Si vuelve `blocks`, contalo en lenguaje de negocio y corregí:
   - `keyword_rule_clashes_with_campaign` o `keyword_rule_clashes_with_rule`: el texto choca (es igual o uno contiene al otro). Proponé otro.
   - `keyword_rule_label_reserved` o `keyword_rule_macro_reserved`: esa etiqueta o ese macro ya los usa el producto o una campaña.
   - `keyword_rule_macro_does_more_than_add_the_label`: el macro hace algo más; hay que usar uno que solo sume la etiqueta.
4. `propose_keyword_rule` con lo mismo y `purpose` (para qué, en una línea, sin teléfonos). Mandá el `approval_url` y el link `wa.me` de la vista previa para el botón: «la aprueba alguien de la lista en esta página; hasta entonces no pone ninguna etiqueta».
5. Cómo va: `get_keyword_rule`. Si `serving` no es `served`, decí por qué (por ejemplo, choca con una campaña que se publicó después).
