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
        "validación (sin el botón, el equipo técnico). Para rehacer un borrador, mandá su proposal_id y su "
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

# template-request-v1 (F-146, parte 5): pedirle una plantilla a Meta. Guarda y valida; quien la manda es
# una persona de la lista, desde la pagina. Ninguna de estas habla con Meta.
_REQUEST_ID = {"type": "string", "format": "uuid"}

PROPOSE_TEMPLATE = {
    "name": "propose_template",
    "description": (
        "Guarda el borrador de una plantilla de WhatsApp que todavía no existe, para mandarla a aprobar a "
        "Meta (template-request-v1). Corre las reglas: con bloqueos no guarda y los devuelve para "
        "corregirlos. La respuesta trae request_id, revision, variables (el orden en que quedan, el mismo "
        "que va en el paso del flujo), warnings y burbuja (el mensaje como lo ve la persona). No manda "
        "nada a Meta. Sin el flag de la instancia contesta template_requests_not_enabled. Para rehacer un "
        "borrador, mandá su request_id y su revision. La guía es pedir-plantilla-a-meta."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "template": {
                "type": "object",
                "description": "nombre (<prefijo de la instancia>_<para qué>_01), idioma (el de las plantillas de "
                               "la instancia), categoria (UTILITY o MARKETING), cuerpo (con los datos entre "
                               "llaves y con nombre: {{nombre}}, {{producto}}), encabezado y pie (opcionales, "
                               "texto fijo) y botones: lista de {tipo: checkout, texto}, {tipo: ficha, texto}, "
                               "{tipo: url_fija, texto, url} o {tipo: respuesta, texto}. El de link va primero.",
            },
            "purpose": {
                "type": "string",
                "minLength": 1,
                "maxLength": 500,
                "description": "Para qué es la plantilla, en una línea, como lo pidió la persona. Sin teléfonos ni mails.",
            },
            "for_flow": {
                "type": "object",
                "description": "Si es para un flujo guardado: {proposal_id, step}, con step el JSON Pointer del paso "
                               "que la espera con {a_crear: <este nombre>}, por ejemplo /items/0/definicion/pasos/0.",
                "properties": {"proposal_id": _FLOW_ID, "step": {"type": "string"}},
                "required": ["proposal_id", "step"],
                "additionalProperties": False,
            },
            "request_id": {**_REQUEST_ID, "description": "Para rehacer un borrador: su id."},
            "revision": {"type": "integer", "minimum": 1, "description": "Para rehacer: la revisión que leíste."},
        },
        "required": ["template", "purpose"],
        "additionalProperties": False,
    },
}

VALIDATE_TEMPLATE = {
    "name": "validate_template",
    "description": (
        "Valida un borrador guardado y lo congela por 24 horas para que una persona de la lista lo revise "
        "y lo mande a Meta desde la página. Si queda validada y sin bloqueos, deja en el chat el botón "
        "para abrir esa página (approval_button). No manda nada a Meta."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "request_id": {**_REQUEST_ID, "description": "El id de la solicitud."},
            "revision": {"type": "integer", "minimum": 1, "description": "La revisión del borrador."},
        },
        "required": ["request_id", "revision"],
        "additionalProperties": False,
    },
}

LIST_TEMPLATE_REQUESTS = {
    "name": "list_template_requests",
    "description": (
        "Lista las solicitudes de plantilla de la instancia, de la más nueva a la más vieja. Lo que le contás "
        "a la persona sale de resolved.state, que el sistema ya cruzó con el catálogo de WhatsApp; status es "
        "el estado interno y no se lo digas: «aprobada» ahí quiere decir que la aprobó una persona y NO se "
        "mandó a Meta, y «enviada» puede ser un envío que falló (last_send_error). resolved.state sin_confirmar "
        "o catalog_checked en falso: traela con get_template_request antes de afirmar nada. Con estado: "
        "abiertas (el default) trae las vivas; todas, también las cerradas. Solo lee. Antes de contestar con "
        "esto, cargá la guía pedir-plantilla-a-meta."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "estado": {"type": "string", "enum": ["abiertas", "todas"], "description": "abiertas (default) o todas."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Cuántas, de 1 a 50 (default 20)."},
        },
        "additionalProperties": False,
    },
}

GET_TEMPLATE_REQUEST = {
    "name": "get_template_request",
    "description": (
        "Relee una solicitud de plantilla: el borrador con sus variables en orden, para qué flujo es, la "
        "categoría que pidió y la que dio Meta, el motivo de un rechazo si se conoce, la burbuja, y "
        "resolved.state: cómo va de verdad, ya cruzado con el catálogo de WhatsApp. Lo que le contás a la "
        "persona sale de resolved.state, no de status («aprobada» en status quiere decir que la aprobó una "
        "persona y NO se mandó a Meta). Si el envío falló trae last_send_error, send_attempts y "
        "send_error_text (el motivo en palabras). Solo lee. La guía es pedir-plantilla-a-meta."
    ),
    "parameters": {
        "type": "object",
        "properties": {"request_id": {**_REQUEST_ID, "description": "El id de la solicitud."}},
        "required": ["request_id"],
        "additionalProperties": False,
    },
}

GET_APPROVAL_PAGE = {
    "name": "get_approval_page",
    "description": (
        "El link de la página de aprobación de esta instancia, para pasárselo a la persona: ahí ve todos los "
        "flujos, los pausa y carga su teléfono al grupo de prueba, sin una propuesta abierta. Entra con "
        "«Entrar con Slack». Con approval_page_not_enabled, esta instancia no tiene la página."
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

# automation-expert-chatwoot-v1 (F-143): la herramienta acotada de Chatwoot. Lee y crea; no borra,
# no saca etiquetas y no manda mensajes.
_LABEL_TITLE = {"type": "string", "minLength": 1, "maxLength": 40,
                "description": "El nombre de la etiqueta: letras, números, guion o guion bajo, en minúsculas "
                               "(Chatwoot la pasa a minúsculas). No puede empezar con automation_."}

LIST_CHATWOOT_LABELS = {
    "name": "list_chatwoot_labels",
    "description": (
        "Lista las etiquetas de la cuenta de Chatwoot de esta instancia: título, descripción, color y si "
        "se ve en la barra lateral. Usala antes de crear una, para no repetirla, y antes de ponerle una a "
        "una conversación. Solo lee."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}

CREATE_CHATWOOT_LABEL = {
    "name": "create_chatwoot_label",
    "description": (
        "Crea una etiqueta en la cuenta de Chatwoot de esta instancia (por ejemplo cliente, la que pone "
        "el paso etiquetar). Si ya existe, no la toca y vuelve con created: false. No la pone en ninguna "
        "conversación. No hay forma de borrar una etiqueta desde acá."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": _LABEL_TITLE,
            "description": {"type": "string", "maxLength": 200,
                            "description": "Para qué es, en una línea. Sin teléfonos ni mails."},
            "color": {"type": "string", "pattern": "^#[0-9a-fA-F]{6}$",
                      "description": "El color en hexadecimal (default #1f93ff)."},
            "show_on_sidebar": {"type": "boolean", "description": "Si se ve en la barra lateral (default true)."},
        },
        "required": ["title"],
        "additionalProperties": False,
    },
}

ADD_CHATWOOT_LABEL_TO_CONVERSATION = {
    "name": "add_chatwoot_label_to_conversation",
    "description": (
        "Le suma una etiqueta que ya existe a una conversación de la bandeja de esta instancia, por su "
        "número de conversación. No saca ninguna otra etiqueta y no manda nada a la persona. Si ya la "
        "tenía, vuelve con changed: false. Usala solo si quien te habla lo pidió en la charla."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "conversation_id": {"type": "integer", "minimum": 1, "description": "El número de la conversación en Chatwoot."},
            "label": _LABEL_TITLE,
        },
        "required": ["conversation_id", "label"],
        "additionalProperties": False,
    },
}

LIST_CHATWOOT_MACROS = {
    "name": "list_chatwoot_macros",
    "description": (
        "Lista los macros de la cuenta de Chatwoot de esta instancia: id, nombre, visibilidad y sus "
        "acciones (por ejemplo add_label con su etiqueta). Sirve para decir qué macro usa la instancia "
        "para pausar, reanudar, dar de baja o poner la etiqueta cliente. Solo lee: no ejecuta ni crea macros."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}

CREATE_CHATWOOT_LABEL_MACRO = {
    "name": "create_chatwoot_label_macro",
    "description": (
        "Crea en Chatwoot un macro que solo suma una etiqueta que ya existe (creala antes con "
        "create_chatwoot_label). Si ya hay un macro que hace exactamente eso, lo devuelve con created: false. "
        "Devuelve su id: es macro_etiqueta de una campaña. No crea otro tipo de macro. "
        "Llamala solo después de que list_campaigns contestó: sin campañas prendidas, no existe."
    ),
    "parameters": {
        "type": "object",
        "properties": {"label": _LABEL_TITLE},
        "required": ["label"],
        "additionalProperties": False,
    },
}

_CAMPAIGN = {
    "type": "object",
    "description": (
        "La campaña: clave (corta, minúsculas y guiones), nombre (lo que titula el bloque), etiqueta y "
        "macro_etiqueta (de create_chatwoot_label_macro), desde y hasta (fecha y hora con zona, por ejemplo "
        "2026-11-20T00:00:00-06:00; hasta es exclusivo), palabras_clave (lista de 1 a 8) y/o pregunta_entrada "
        "(una línea). instruccion (opcional, hasta 600 caracteres); oferta (opcional: producto, precio y moneda "
        "juntos, precio_antes, cupon, codigo_oferta, incluye, garantia, medios, reemplaza). codigo_oferta es la "
        "oferta de Hotmart de la campaña, si tiene una propia (el off= del link de pago de su página): tiene que "
        "estar declarada en la instancia, no ser la de siempre y la instancia tiene que tener prendido el link con la "
        "oferta de la campaña; con ella el link del agente sale con esa oferta. asesoramiento (lista, "
        "obligatoria); "
        "se_puede_decir, no_confirmado, promesas_prohibidas (listas opcionales); faq y preguntas_del_alta "
        "(listas de {pregunta, respuesta, fuente}). Los precios por país van adentro de la oferta o de "
        "se_puede_decir, no en otra campaña."
    ),
}

PREVIEW_CAMPAIGN = {
    "name": "preview_campaign",
    "description": (
        "Valida una campaña contra esta instancia y muestra cómo la va a leer el agente, sin guardar nada. "
        "Vuelve con ok, blocks (qué corregir), warnings y preview (render, vigencia, palabras clave y el link de "
        "WhatsApp para el botón)."
    ),
    "parameters": {
        "type": "object",
        "properties": {"campaign": _CAMPAIGN},
        "required": ["campaign"],
        "additionalProperties": False,
    },
}

PROPOSE_CAMPAIGN = {
    "name": "propose_campaign",
    "description": (
        "Guarda o rehace el borrador de una campaña. No la publica: nadie la recibe hasta que un aprobador la "
        "apruebe en la página. Para rehacer, mandá campaign_id y revision; una validada también se puede rehacer "
        "(por ejemplo, si el control de calidad dejó preguntas)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "campaign": _CAMPAIGN,
            "purpose": {"type": "string", "minLength": 1, "maxLength": 500,
                        "description": "Para qué es, en una línea, como lo pidió la persona. Sin teléfonos ni mails."},
            "campaign_id": {"type": "string", "format": "uuid"},
            "revision": {"type": "integer", "minimum": 1},
        },
        "required": ["campaign", "purpose"],
        "additionalProperties": False,
    },
}

VALIDATE_CAMPAIGN = {
    "name": "validate_campaign",
    "description": (
        "Valida el borrador guardado: revisa la instancia y que el macro sume exactamente la etiqueta. Sin "
        "bloqueos queda congelado 24 horas y hay que correrle el control de calidad (lo hace el equipo). Después, "
        "un aprobador la abre en la página y la publica. Solo valida."
    ),
    "parameters": {
        "type": "object",
        "properties": {"campaign_id": {"type": "string", "format": "uuid"}, "revision": {"type": "integer", "minimum": 1}},
        "required": ["campaign_id", "revision"],
        "additionalProperties": False,
    },
}

GET_CAMPAIGN = {
    "name": "get_campaign",
    "description": (
        "Relee una campaña: su estado, la vista previa, lo que dio el control de calidad (check_report: veredicto, "
        "items y, si trae criterio, avisos que no frenan) y si tiene un control limpio anotado (check_clean). "
        "check_clean no asegura el botón de publicar: lo decide la página, según el conocimiento vigente. Solo lee."
    ),
    "parameters": {
        "type": "object",
        "properties": {"campaign_id": {"type": "string", "format": "uuid"}},
        "required": ["campaign_id"],
        "additionalProperties": False,
    },
}

LIST_CAMPAIGNS = {
    "name": "list_campaigns",
    "description": (
        "Las campañas de la instancia y en qué estado están. estado: abiertas (por defecto) o todas. Solo lee. "
        "Con campaigns_not_enabled las campañas no están prendidas en esta instancia: no crees etiqueta ni macro."
    ),
    "parameters": {
        "type": "object",
        "properties": {"estado": {"type": "string", "enum": ["abiertas", "todas"]}},
        "additionalProperties": False,
    },
}
