---
name: editar-el-conocimiento
description: "Cambiar lo que sabe el agente comercial"
---

# Editar el conocimiento del agente comercial

**Cargala antes de tu primera respuesta si alguien quiere cambiar lo que el agente comercial sabe o dice siempre**: una pregunta frecuente, una regla de cómo habla, lo que incluye el programa, lo que no está confirmado. Por ejemplo: «sumale al agente que la garantía es de 7 días», «que deje de decir que hay clases en vivo», «agregá esta pregunta frecuente». Si es algo por unos días o para una campaña, no es esto: es una campaña (`armar-una-campana`).

## Primero: ¿está prendido en esta instancia?

**Tu primera herramienta es `get_knowledge`.** Si vuelve `knowledge_not_enabled`, esto no está prendido acá: el cambio lo hace el equipo en el repositorio de la instancia. Decilo así y frená; podés ofrecer anotar lo que quiere.

## Lo que esto hace, y lo que no

Dicho en una frase: **lo armo acá, lo aprueba alguien de la lista en una página, y el agente lo usa desde la respuesta siguiente, sin subir nada al servidor.** Se puede volver al conocimiento de siempre con un botón de la misma página.

- **No manda nada a nadie.** Cambia lo que el agente sabe, en todas las conversaciones.
- **No aprobás vos.** Lo aprueba una persona de la lista en la página.
- **No se puede aprobar mientras haya una campaña andando** (o un segmento en vigencia): se revisó contra el conocimiento de hoy. Si es urgente durante una campaña, va por la instrucción temporal. Si `get_knowledge` dice que hay una versión aprobada que no se está usando (`no_servida`), contalo.
- **El precio no se cambia acá**: sale del catálogo de la instancia. Si te piden otro precio o un monto nuevo, decí que primero lo tiene que declarar el equipo en la instancia.
- **El nombre del producto y los productos tampoco**: eso es un cambio de la instancia.

## Cómo se conversa

Las reglas de `crear-un-flujo`: lenguaje de negocio, mensajes cortos, una o dos preguntas por mensaje y, si ya lo dijo, no lo preguntes. No inventes datos: lo que no se sabe va en `no_confirmado` (el agente lo deriva).

## Los pasos

1. **Leé lo vigente** con `get_knowledge`: `secciones` trae cada sección tal cual. Copiá la que cambia **entera** y cambiá solo lo pedido. Lo que no nombrás queda igual.
2. **La vista previa.** `preview_knowledge_change` con `cambios` (por ejemplo `{"faq": [...]}`) valida con las reglas de siempre y devuelve el `diff`. Si vuelve con `blocks`, corregí vos y probá de nuevo; `knowledge_amount_not_in_catalog` o `knowledge_price_not_in_catalog` son un monto que no está en el catálogo: sacalo.
3. **El resumen.** Contá en palabras qué cambia (lo nuevo y lo que deja de decir, del `diff`) y **esperá el sí**.
4. **Guardar.** `propose_knowledge_change` con los mismos `cambios` y `purpose` en una línea (qué cambia y por qué). Queda esperando el sí 24 horas.
5. **El link.** Mandá el `approval_url` tal cual: «Ya quedó armado. Lo aprueba alguien de la lista en esta página y el agente lo usa desde ahí; nadie lo recibe antes.» No digas que ya quedó andando.

## Después

- **Si preguntan cómo va**, `get_knowledge_change` (con el `knowledge_id`) o `list_knowledge_changes`: el estado y, si está aprobada, si el agente la está usando (`serving`).
- **Si vuelve `agent_knowledge_base_changed`**, lo vigente cambió mientras tanto (se aprobó otra o se volvió a lo de siempre): leé de nuevo con `get_knowledge` y armalo otra vez.
- **Una versión aprobada no se edita**: se arma la siguiente sobre ella. Volver a lo de siempre lo hace un aprobador en la página.
- **El arnés de conversaciones no corre desde acá** (`knowledge_harness_not_run`): sugerí probarlo con el equipo después de aprobarlo.
