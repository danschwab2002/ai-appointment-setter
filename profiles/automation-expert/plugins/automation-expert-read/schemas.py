"""Los esquemas de las herramientas de lectura del Automation Expert (automation-expert-read-v1)."""

LIST_FLOWS = {
    "name": "list_flows",
    "description": (
        "Lista los flujos de la instancia (primer contacto, carrito, pago fallido, "
        "ficha de pago, reactivación y cupón, más los flujos nativos del motor, como la "
        "bienvenida, con native: true, su estado (cohort = solo la cohorte de prueba) y su "
        "cohorte) con su versión, si están prendidos, sus topes, lo que pasó en los últimos "
        "7 días y, para los flujos del motor, sus ventas (`sales`: compras después de un "
        "envío aceptado, con la regla en `sales.rule`), más lo que corre en código y los "
        "eventos que no disparan nada. `business_flows` dice, por flujo de negocio, quién lo "
        "ejecuta hoy (`executor`: legado, nativo, nativo_en_cohorte, los dos, motor_v1 o "
        "nadie), por qué scope sigue el legado y qué nativos lo hacen, con un `summary` para "
        "contarlo. Un flujo del legado con ruta del motor v1 trae `engine_v1`: su ruta, sus "
        "divergencias y sus rechazos. Con flow_key devuelve uno solo. Solo lee."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "flow_key": {
                "type": "string",
                "pattern": "^[a-z][a-z0-9_]{1,39}$",
                "description": "La clave de un flujo (precheckout, carrito, pago_fallido, ficha, reactivacion, descuento o la de un flujo nativo), para pedir solo ese.",
            }
        },
        "additionalProperties": False,
    },
}

DRAW_FLOW = {
    "name": "draw_flow",
    "description": (
        "Dibuja los flujos de la instancia como en un editor (disparador, esperas, "
        "plantillas, fin), en un HTML que se adjunta en Slack. Con flow_key ese flujo "
        "queda adelante. Devuelve `media`: copiá esa línea MEDIA:... tal cual, sola, al "
        "final de tu respuesta, y Slack adjunta el archivo. El dibujo sale de la misma "
        "lectura que list_flows: decí también los huecos que trae (`gaps`). Solo lee."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "flow_key": {
                "type": "string",
                "pattern": "^[a-z][a-z0-9_]{1,39}$",
                "description": "El flujo que se abre primero en el dibujo.",
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

READ_LIBRARY = {
    "name": "read_library",
    "description": (
        "La biblioteca de plataformas: sin tema devuelve el índice (los temas y los eventos en una "
        "línea); con tema, la página: hotmart (los eventos y el producto por ID), ghl-formularios "
        "(cómo llega el formulario con permiso de WhatsApp) o chatwoot-whatsapp (plantillas, "
        "categorías, la ventana de 24 h y las etiquetas). Usala antes de armar un flujo nuevo para "
        "saber si el evento existe y llega. No trae nada de la instancia ni de una persona. Solo lee."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "tema": {
                "type": "string",
                "enum": ["hotmart", "ghl-formularios", "chatwoot-whatsapp"],
                "description": "El tema; sin tema, el índice.",
            }
        },
        "additionalProperties": False,
    },
}
