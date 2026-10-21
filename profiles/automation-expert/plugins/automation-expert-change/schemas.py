"""Los esquemas de las cuatro herramientas de cambio del Automation Expert (flow-change-cycle-v1)."""

_FLOW_ID = {"type": "string", "format": "uuid"}

GET_FLOW_DEFINITIONS = {
    "name": "get_flow_definitions",
    "description": (
        "Lee los flujos del almacén: su flow_id, su estado, su versión activa, las versiones que "
        "tiene y la definición activa completa (setter-flujo/v1). Es de donde se copia el flujo "
        "antes de proponer un cambio. Solo lee."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}

PROPOSE_FLOW_CHANGE = {
    "name": "propose_flow_change",
    "description": (
        "Guarda un borrador de cambio de uno o varios flujos, con el formato setter-flujo/v1: una "
        "versión nueva (la definición completa), volver a una versión publicada, prender o apagar. "
        "Antes, leé el flujo con get_flow_definitions y copiá su definición activa: el cambio se escribe "
        "sobre esa. En base va la versión activa que leíste de cada flujo. Si hay bloqueos, no "
        "guarda y los devuelve para corregirlos. No aprueba ni publica: eso lo hace un aprobador "
        "con el comando del operador. Para rehacer un borrador, mandá su proposal_id y su revision."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "minItems": 1,
                "maxItems": 10,
                "description": "Los ítems: {producto, flujo, flow_id, tipo: version|volver|prender|apagar, "
                               "definicion (si es version), version (si es volver), en_curso: seguir}.",
                "items": {"type": "object"},
            },
            "base": {
                "type": "object",
                "description": "Por cada flow_id de los ítems, {active_version: n | null}: la versión que leíste.",
            },
            "purpose": {
                "type": "string",
                "minLength": 1,
                "maxLength": 500,
                "description": "Para qué es el cambio, en una línea, como lo pidió la persona. Sin teléfonos ni mails.",
            },
            "proposal_id": {**_FLOW_ID, "description": "Para rehacer un borrador: su id."},
            "revision": {"type": "integer", "minimum": 1, "description": "Para rehacer: la revisión que leíste."},
        },
        "required": ["items", "base", "purpose"],
        "additionalProperties": False,
    },
}

VALIDATE_FLOW_CHANGE = {
    "name": "validate_flow_change",
    "description": (
        "Valida un borrador guardado contra el catálogo de plantillas y lo deja listo para que un "
        "aprobador lo publique: devuelve la vista previa (qué cambia, campo por campo, a qué se "
        "vuelve si sale mal) y lo que todavía no se sabe (la simulación, lo que está en curso). "
        "Vence a las 24 horas. No publica."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "proposal_id": {**_FLOW_ID, "description": "El id del borrador."},
            "revision": {"type": "integer", "minimum": 1, "description": "La revisión del borrador."},
        },
        "required": ["proposal_id", "revision"],
        "additionalProperties": False,
    },
}

GET_FLOW_CHANGE = {
    "name": "get_flow_change",
    "description": "Relee una propuesta: su estado, sus ítems, su vista previa y sus últimos eventos. Solo lee.",
    "parameters": {
        "type": "object",
        "properties": {"proposal_id": {**_FLOW_ID, "description": "El id de la propuesta."}},
        "required": ["proposal_id"],
        "additionalProperties": False,
    },
}
