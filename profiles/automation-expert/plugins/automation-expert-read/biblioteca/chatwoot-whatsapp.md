# Chatwoot y WhatsApp

**Por dónde sale todo.** Cada mensaje sale por la bandeja de WhatsApp de la instancia en Chatwoot. «Salió» quiere decir que Chatwoot aceptó el mensaje (`accepted_by_chatwoot`); si llegó lo dice Meta después, en la entrega (entregado, leído o fallido).

**Las plantillas.** Para escribirle primero a alguien hace falta una plantilla aprobada por Meta en esa bandeja. El bridge lee el catálogo de la bandeja en cada envío: una plantilla que Meta pausó o rechazó corta el envío. Cada plantilla tiene su idioma (por ejemplo `es_MX`), sus variables (`{{1}}`, `{{2}}`, …) y su categoría:

- `MARKETING`: la de la recuperación (el carrito, el cupón). Meta puede no entregarla si a la persona le llegan muchos mensajes de marketing (código 131049).
- `UTILITY`: la de un mensaje que la persona espera, como la confirmación de la compra. Con `permiso: compra` un flujo solo puede mandar una plantilla de utilidad, una vez (regla 17).

Una plantilla que no existe se escribe como `{a_crear: <nombre>}`: el flujo se guarda pero no se publica hasta que Meta la apruebe.

**La plantilla de un flujo de la compra** (un flujo nativo) manda las variables que el paso escribe en `variables`: una por marcador, en su orden, de entre `nombre` (el de quien compró, como viene de Hotmart) y `producto`. Por ejemplo, una plantilla cuyo único marcador es el producto lleva `variables: ["producto"]`. El catálogo no se lista desde acá: el nombre de la plantilla y qué es cada marcador los da quien la pidió. La validación compara `variables` con la plantilla aprobada y, si no coinciden, `template_variable_count_mismatch` dice cuántos marcadores tiene.

**Los botones de esa plantilla.** Puede tener botones con un link fijo (por ejemplo, «Completar mi perfil» a un formulario): salen tal cual, sin nada que llenar. Un botón de respuesta rápida, o uno con un link que cambia por persona, la frena (`template_button_mismatch`, con el motivo): no se corrige en el pedido, hay que pedirle a Meta otra plantilla.

**La ventana de 24 horas.** Después del último mensaje de la persona, durante 24 horas se le puede escribir texto libre; pasadas las 24 horas, solo una plantilla aprobada (Meta lo rechaza con 131047).

**Las etiquetas.** El paso `etiquetar: cliente` de un flujo disparado por la compra le pone la etiqueta `cliente` a la conversación y deja una nota privada con el producto y la fecha. No manda nada a la persona. Si la instancia no tiene cargado el macro de la etiqueta, el paso deja solo la nota.

**Una conversación tomada por el equipo** (asignada a una persona, o con la automatización en pausa) frena los flujos: el agente no le escribe mientras la tenga el equipo.
