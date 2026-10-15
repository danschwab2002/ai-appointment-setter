"""Los esquemas de las dos herramientas del Automation Expert (automation-expert-read-v1)."""

LIST_FLOWS = {
    "name": "list_flows",
    "description": (
        "Lista los flujos de la instancia (primer contacto, carrito, pago fallido, "
        "reactivación y cupón) con su versión, si están prendidos, sus topes y lo que "
        "pasó en los últimos 7 días, más lo que corre en código y los eventos que no "
        "disparan nada. Con flow_key devuelve uno solo. Solo lee."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "flow_key": {
                "type": "string",
                "enum": ["precheckout", "carrito", "pago_fallido", "reactivacion", "descuento"],
                "description": "La clave de un flujo, para pedir solo ese.",
            }
        },
        "additionalProperties": False,
    },
}

LEAD_HISTORY = {
    "name": "lead_history",
    "description": (
        "Cuenta qué le pasó o le va a pasar a un lead y por qué: formulario, eventos de "
        "Hotmart, flujos, plantillas, entrega de Meta, respuestas, derivaciones y "
        "compra. Se pide con UNA llave: el número de conversación de Chatwoot, el link "
        "de pago pegado, o el nombre (si hay varios, devuelve candidatos para "
        "preguntar). Nunca se busca por teléfono ni por mail. Con lead_ref se pide la "
        "historia de un candidato ya elegido. Solo lee."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "conversation": {
                "type": "integer",
                "minimum": 1,
                "maximum": 999999,
                "description": "El número de conversación de Chatwoot (el de la URL).",
            },
            "link": {
                "type": "string",
                "description": "El link de pago que se le mandó. Del link sale solo el identificador de la emisión.",
            },
            "name": {
                "type": "string",
                "minLength": 3,
                "maxLength": 80,
                "description": "El nombre de la persona, o el principio de su primer nombre.",
            },
            "lead_ref": {
                "type": "string",
                "format": "uuid",
                "description": "El lead_ref de un candidato devuelto por una búsqueda anterior.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "default": 50,
                "description": "Cuántos momentos traer por página.",
            },
            "cursor": {
                "type": "string",
                "description": "El next_cursor de la página anterior.",
            },
        },
        "additionalProperties": False,
    },
}
