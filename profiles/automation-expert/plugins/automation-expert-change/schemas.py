"""Los esquemas de las herramientas de cambio del Automation Expert (flow-change-cycle-v1)."""

_FLOW_ID = {"type": "string", "format": "uuid"}

GET_FLOW_DEFINITIONS = {
    "name": "get_flow_definitions",
    "description": (
        "Lee los flujos del almacén: su flow_id, su estado, su versión activa, las versiones que "
        "tiene, la definición activa completa (setter-flujo/v1) y sus toques (cuándo sale cada "
        "mensaje, en horas desde el primero). Cada flujo trae draft_item y draft_base: el ítem y la "
        "base de propose_flow_change, listos para copiar. Solo lee."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}

PROPOSE_FLOW_CHANGE = {
    "name": "propose_flow_change",
    "description": (
        "Guarda un borrador de cambio de uno o varios flujos, con el formato setter-flujo/v1: una "
        "versión nueva (la definición completa), volver a una versión publicada, prender o apagar, o un "
        "flujo nuevo (tipo nuevo, sin flow_id, con prender: cohorte y base vacía: se publica solo para la "
        "cohorte de prueba). "
        "Para una versión nueva: copiá draft_item de get_flow_definitions en items, cambiá solo lo "
        "pedido en draft_item.definicion.pasos, y mandá draft_base en base. La respuesta trae toques: "
        "cuándo sale cada mensaje antes y después, en horas desde el primero; comparalos con lo "
        "pedido. Si hay bloqueos, no guarda y los devuelve para corregirlos; si el pedido no cierra, "
        "reason dice qué campo, qué ítem y qué claves faltan o sobran. No aprueba ni publica: eso lo "
        "hace un aprobador desde la página de aprobación, a la que lleva el botón que deja la "
        "validación (sin el botón, el operador). Para rehacer un borrador, mandá su proposal_id y su "
        "revision."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "minItems": 1,
                "maxItems": 10,
                "description": "Los ítems, con estas claves exactas: version → {producto, flujo, flow_id, "
                               "tipo: \"version\", definicion, en_curso: \"seguir\"} (es draft_item); "
                               "volver → {producto, flujo, flow_id, tipo: \"volver\", version, en_curso}; "
                               "prender o apagar → {producto, flujo, flow_id, tipo}; volver un flujo nativo "
                               "pausado a la cohorte de prueba → {producto, flujo, flow_id, tipo: \"prender\", "
                               "a: \"cohorte\"}; un flujo nuevo → "
                               "{producto, flujo, tipo: \"nuevo\", prender: \"cohorte\", definicion} y, con "
                               "permiso: compra, ok_del_cliente {quien, medio, fecha}. producto y flujo son el "
                               "product_ref y el flow_key del almacén.",
                "items": {"type": "object"},
            },
            "base": {
                "type": "object",
                "description": "Por cada flow_id de los ítems, {active_version: n | null}: la versión que "
                               "leíste (es draft_base). Un flujo nuevo no suma nada: solo, va {}.",
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

LIST_FLOW_CHANGES = {
    "name": "list_flow_changes",
    "description": (
        "Lista las propuestas de cambio de flujo de la instancia, de la más nueva a la más vieja: su "
        "proposal_id, su estado (borrador, validada, aprobada, publicada, descartada o vencida; "
        "effective_status dice vencida si pasaron las 24 horas), qué flujos toca, para qué era (solo "
        "en las tuyas), las fechas y el último evento. Con estado: abiertas (el default) trae solo las "
        "que siguen vivas, y open dice cuántas abiertas tenés de las 10 que podés tener. Usala para "
        "retomar una propuesta de otra charla: con su proposal_id, get_flow_change la trae entera. "
        "Solo lee."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "estado": {"type": "string", "enum": ["abiertas", "todas"],
                       "description": "abiertas (default): borradores y validadas o aprobadas sin vencer; todas: también las cerradas."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Cuántas, de 1 a 50 (default 20)."},
        },
        "additionalProperties": False,
    },
}

LIST_TEMPLATES = {
    "name": "list_templates",
    "description": (
        "Lista el catálogo de plantillas de WhatsApp de la bandeja, el mismo que mira la validación: "
        "nombre, idioma, estado (solo una APPROVED se puede usar), categoría (UTILITY o MARKETING), "
        "cuántos marcadores tiene el cuerpo ({{1}}, {{2}}…: uno por variable del paso, en su orden), "
        "el texto del encabezado, del cuerpo y del pie, y los botones (link_cambia: el link lleva un "
        "{{1}} y cambia por persona). Primero las aprobadas. Usala antes de elegir la plantilla de un "
        "paso y para escribir sus variables. Solo lee."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
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
