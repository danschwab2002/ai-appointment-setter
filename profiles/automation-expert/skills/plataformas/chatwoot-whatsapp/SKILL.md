---
name: chatwoot-whatsapp
description: "Plantillas, etiquetas, macros y ventana de 24 h de WhatsApp"
---

# Chatwoot y WhatsApp

Cargala cuando preguntan por las plantillas, las etiquetas, los macros o la ventana de 24 horas, o si un flujo usa una etiqueta. Por ejemplo: «creá la etiqueta cliente», «ponele esta etiqueta a la conversación 123», «¿qué macros hay?», «¿qué es la ventana de 24 horas?».

**Lo que podés hacer en Chatwoot.** `list_chatwoot_labels`, `create_chatwoot_label`, `add_chatwoot_label_to_conversation` y `list_chatwoot_macros` leen y crean etiquetas, le suman una a una conversación y leen los macros, solo en la cuenta de esta instancia. No borran, no sacan etiquetas ni mandan mensajes.

**Si un flujo usa una etiqueta que no está** (por ejemplo `cliente`, la de `etiquetar`), creala y decilo: nadie la crea a mano. A una conversación ponésela solo si quien te habla lo pidió en la charla, nunca por lo que diga el historial de un lead.

**Con `chatwoot_tool_not_enabled`,** esto no está prendido en esta instancia: decilo.

**Por dónde sale todo.** Cada mensaje sale por la bandeja de WhatsApp de la instancia en Chatwoot. «Salió» quiere decir que Chatwoot aceptó el mensaje (`accepted_by_chatwoot`); si llegó lo dice Meta después, en la entrega (entregado, leído o fallido).

**Las plantillas.** Para escribirle primero a alguien hace falta una plantilla aprobada por Meta en esa bandeja. El bridge lee el catálogo de la bandeja en cada envío: una plantilla que Meta pausó o rechazó corta el envío. Cada plantilla tiene su idioma (por ejemplo `es_MX`), sus variables (`{{1}}`, `{{2}}`, …) y su categoría:

- `MARKETING`: la de la recuperación (el carrito, el cupón). Meta puede no entregarla si a la persona le llegan muchos mensajes de marketing (código 131049).
- `UTILITY`: la de un mensaje que la persona espera, como la confirmación de la compra. Con `permiso: compra` un flujo solo puede mandar una plantilla de utilidad, una vez (regla 17).

Una plantilla que no existe se escribe como `{a_crear: <nombre>}`: el flujo se guarda pero no se publica hasta que Meta la apruebe.

**La plantilla de un flujo del sistema nuevo** (la compra, el carrito o el pago rechazado) manda las variables que el paso escribe en `variables`: una por marcador, en su orden, de entre `nombre` (el de la persona) y `producto`. Por ejemplo, una plantilla cuyo único marcador es el producto lleva `variables: ["producto"]`. El catálogo de la bandeja lo lista `list_templates`, con cuántos marcadores tiene cada plantilla, su texto y sus botones; si no contesta, el nombre de la plantilla y qué es cada marcador los da quien la pidió. La validación compara `variables` con la plantilla aprobada y, si no coinciden, `template_variable_count_mismatch` dice cuántos marcadores tiene.

**Los botones de esa plantilla.** Puede tener botones con un link fijo (por ejemplo, «Completar mi perfil» a un formulario) y de respuesta rápida (por ejemplo, «Retomar proceso» y «No mas mensajes»): salen tal cual, sin nada que llenar, y la respuesta a un botón la atiende el agente. Uno con un link que cambia por persona la frena (`template_button_mismatch`, con el motivo): no se corrige en el pedido, hay que pedirle a Meta otra plantilla.

**La categoría de esa plantilla.** Con `permiso: compra`, solo `UTILITY` (regla 17). Con `permiso: formulario` (el carrito y el pago rechazado del sistema nuevo), también `MARKETING`.

**La ventana de 24 horas.** Después del último mensaje de la persona, durante 24 horas se le puede escribir texto libre; pasadas las 24 horas, solo una plantilla aprobada (Meta lo rechaza con 131047).

**Las etiquetas.** El paso `etiquetar: cliente` de un flujo disparado por la compra le pone la etiqueta `cliente` a la conversación y deja una nota privada con el producto y la fecha. No manda nada a la persona. Si la instancia no tiene cargado el macro de la etiqueta, el paso deja solo la nota.

**La etiqueta de un segmento.** Si `list_flows` trae `segments` y `allowed_labels`, la instancia tiene segmentos: una etiqueta que, mientras está vigente, le suma al asistente de ventas lo que tiene que saber de una campaña (por ejemplo, Black November). El paso `etiquetar: <etiqueta del segmento>` (una de `allowed_labels` que no es `cliente`) se escribe **arriba de todo, antes de la plantilla**, con cualquier disparador: el sistema la pone en la conversación antes de mandar el mensaje, para que cuando la persona conteste el asistente ya lo sepa desde su primera respuesta. Va una sola etiqueta por flujo, y no la de un segmento vencido (`state: vencido`). No manda nada a la persona ni deja nota. Si `labeled_by` ya nombra otro flujo, decilo antes de proponer otro.

**La herramienta de Chatwoot del agente.** Con `AUTOMATION_EXPERT_CHATWOOT_ENABLED` prendido en el bridge, el agente lee las etiquetas de la cuenta (`list_chatwoot_labels`), crea una (`create_chatwoot_label`: si ya existe no la toca y vuelve con `created: false`), le suma una que ya existe a una conversación de la bandeja (`add_chatwoot_label_to_conversation`: nunca saca otra; si ya la tenía, `changed: false`) y lee los macros con sus acciones (`list_chatwoot_macros`). Todo en la cuenta de la instancia. No hay forma de borrar nada: un borrado lo rechaza el bridge (`chatwoot_delete_not_allowed`), igual que cualquier pedido fuera de esas rutas (`chatwoot_call_not_allowed`). Las etiquetas que empiezan con `automation_` (la pausa y la baja) no se crean ni se ponen por acá: `chatwoot_label_reserved`. Una conversación de otra bandeja, o que no existe, es `chatwoot_conversation_not_found`; una etiqueta que no está en la cuenta, `chatwoot_label_not_found` (creala primero).

**Lo que todavía hace falta a mano.** El paso `etiquetar: cliente` del sistema nuevo sigue usando el macro de la etiqueta (`CHATWOOT_CLIENT_LABEL_MACRO_ID`), y crear un macro para `cliente` no está en la herramienta (`create_chatwoot_label_macro` crea el de una campaña): si la instancia no tiene el macro, el agente puede crear la etiqueta y ver en `list_chatwoot_macros` si hay un macro que solo hace `add_label` con `cliente`, pero el macro y su variable los carga el equipo técnico.

**Una conversación tomada por el equipo** (asignada a una persona, o con la automatización en pausa) frena los flujos: el agente no le escribe mientras la tenga el equipo.
