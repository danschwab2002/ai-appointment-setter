# Chatwoot y WhatsApp

**Por dónde sale todo.** Cada mensaje sale por la bandeja de WhatsApp de la instancia en Chatwoot. «Salió» quiere decir que Chatwoot aceptó el mensaje (`accepted_by_chatwoot`); si llegó lo dice Meta después, en la entrega (entregado, leído o fallido).

**Las plantillas.** Para escribirle primero a alguien hace falta una plantilla aprobada por Meta en esa bandeja. El bridge lee el catálogo de la bandeja en cada envío: una plantilla que Meta pausó o rechazó corta el envío. Cada plantilla tiene su idioma (por ejemplo `es_MX`), sus variables (`{{1}}`, `{{2}}`, …) y su categoría:

- `MARKETING`: la de la recuperación (el carrito, el cupón). Meta puede no entregarla si a la persona le llegan muchos mensajes de marketing (código 131049).
- `UTILITY`: la de un mensaje que la persona espera, como la confirmación de la compra. Con `permiso: compra` un flujo solo puede mandar una plantilla de utilidad, una vez (regla 17).

Una plantilla que no existe se escribe como `{a_crear: <nombre>}`: el flujo se guarda pero no se publica hasta que Meta la apruebe.

**La ventana de 24 horas.** Después del último mensaje de la persona, durante 24 horas se le puede escribir texto libre; pasadas las 24 horas, solo una plantilla aprobada (Meta lo rechaza con 131047).

**Las etiquetas.** El paso `etiquetar: cliente` de un flujo disparado por la compra le pone la etiqueta `cliente` a la conversación y deja una nota privada con el producto y la fecha. No manda nada a la persona.

**Una conversación tomada por el equipo** (asignada a una persona, o con la automatización en pausa) frena los flujos: el agente no le escribe mientras la tenga el equipo.
