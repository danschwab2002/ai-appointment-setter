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
        "Devuelve su id: es macro_etiqueta de una campaña o de una regla de palabra clave. No crea otro tipo "
        "de macro. Llamala solo después de que list_campaigns o list_keyword_rules contestó: sin campañas ni "
        "reglas prendidas, no existe."
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

# keyword-rules-v1 (Z1 3.9): la regla «texto del link -> etiqueta».
_KEYWORD_RULE = {
    "type": "object",
    "description": (
        "La regla: clave (corta, minúsculas, dígitos y guiones), nombre (para la página), palabra_clave (el "
        "texto que trae escrito el link de WhatsApp, tal cual; cuenta si el primer mensaje de la conversación lo "
        "trae, sin importar mayúsculas, acentos ni puntuación), etiqueta y macro_etiqueta (de "
        "create_chatwoot_label_macro: un macro que solo suma esa etiqueta). No lleva flujo: la regla solo pone la "
        "etiqueta y le avisa al agente que el mensaje es el texto de un link."
    ),
}

PREVIEW_KEYWORD_RULE = {
    "name": "preview_keyword_rule",
    "description": (
        "Valida una regla de palabra clave contra esta instancia sin guardar nada. Vuelve con ok, blocks (qué "
        "corregir: la palabra choca con una campaña o con otra regla, la etiqueta o el macro ya los usa el "
        "producto, el macro hace algo más que sumar la etiqueta) y preview con el link wa.me para el botón."
    ),
    "parameters": {
        "type": "object",
        "properties": {"rule": _KEYWORD_RULE},
        "required": ["rule"],
        "additionalProperties": False,
    },
}

PROPOSE_KEYWORD_RULE = {
    "name": "propose_keyword_rule",
    "description": (
        "Guarda o rehace el borrador de una regla de palabra clave y devuelve approval_url, el link de la página "
        "donde un aprobador la publica. No la publica: no pone ninguna etiqueta hasta ese sí. Para rehacer, mandá "
        "rule_id y revision."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "rule": _KEYWORD_RULE,
            "purpose": {"type": "string", "minLength": 1, "maxLength": 500,
                        "description": "Para qué es, en una línea, como lo pidió la persona. Sin teléfonos ni mails."},
            "rule_id": {"type": "string", "format": "uuid"},
            "revision": {"type": "integer", "minimum": 1},
        },
        "required": ["rule", "purpose"],
        "additionalProperties": False,
    },
}

GET_KEYWORD_RULE = {
    "name": "get_keyword_rule",
    "description": (
        "Relee una regla de palabra clave: estado (borrador, publicada, pausada, descartada), si se está usando "
        "(serving: served o el motivo de por qué no) y el link de la página. Solo lee."
    ),
    "parameters": {
        "type": "object",
        "properties": {"rule_id": {"type": "string", "format": "uuid"}},
        "required": ["rule_id"],
        "additionalProperties": False,
    },
}

LIST_KEYWORD_RULES = {
    "name": "list_keyword_rules",
    "description": (
        "Las reglas de palabra clave de la instancia y su estado. estado: abiertas (por defecto) o todas. Solo "
        "lee. Con keyword_rules_not_enabled no están prendidas en esta instancia: no crees etiqueta ni macro."
    ),
    "parameters": {
        "type": "object",
        "properties": {"estado": {"type": "string", "enum": ["abiertas", "todas"]}},
        "additionalProperties": False,
    },
}

# E-486 (8d): sumar a alguien al grupo de prueba de un flujo en cohorte.
ADD_TO_TEST_GROUP = {
    "name": "add_to_test_group",
    "description": (
        "Suma a una persona al grupo de prueba (cohorte) de un flujo nativo que está en cohorte. Solo agrega, "
        "hasta 5 personas. Va por el número de conversación de Chatwoot o por el teléfono, uno de los dos: "
        "usalo solo con el dato que te dio un aprobador; nunca pidas un teléfono primero. Con not_an_approver, "
        "quien te habla no es aprobador: pasale el link de get_approval_page. Con cohort_tool_not_enabled, "
        "esta instancia no lo tiene prendido. La respuesta nunca trae el teléfono: no lo repitas."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "product": {"type": "string", "description": "La clave del producto del flujo (product_ref)."},
            "flow": {"type": "string", "description": "La clave del flujo (flow_key)."},
            "conversation_id": {"type": "integer", "minimum": 1,
                                "description": "El número de conversación de Chatwoot de la persona."},
            "phone": {"type": "string", "maxLength": 32,
                      "description": "El teléfono de la persona, con código de país."},
        },
        "required": ["product", "flow"],
        "additionalProperties": False,
    },
}

# Encargo 6 del masivo manejado por el AE (piezas 7 y 11): el borrador y su lectura. Confirmar, arrancar y
# frenar son de la pagina; la lista se sube ahi, nunca en el chat.
PROPOSE_BULK_SEND = {
    "name": "propose_bulk_send",
    "description": (
        "Guarda el borrador de un envío masivo a una lista que trae la persona (CSV, Excel o JSON) y devuelve "
        "upload_url: el link de la página donde un aprobador sube la lista, la revisa y confirma el envío. "
        "Nunca pidas la lista, un teléfono ni un mail en el chat: mandá upload_url tal cual con upload_note. "
        "La plantilla tiene que estar aprobada (APPROVED en list_templates) y tener la respuesta rápida "
        "«No más mensajes»; si no la tiene, vuelve bulk_send_template_without_opt_out y next dice qué ofrecer "
        "(pedirla a Meta con pedir-plantilla-a-meta). template_variables va en el orden de los marcadores "
        "del cuerpo: un texto fijo o {\"de\": \"nombre\"} para el primer nombre de cada persona de la lista. "
        "scheduled_at es la hora central del envío, ISO 8601 con zona (al menos 1 hora y como mucho 30 días "
        "adelante); el envío se reparte alrededor de esa hora (window_mode centrada, el default) o arranca a "
        "esa hora (desde_la_hora). header_image_url solo si la plantilla tiene imagen en el encabezado. "
        "product_refs: los productos del envío (quien ya los compró queda afuera); con un solo producto en la "
        "instancia se puede omitir. Si el pedido no cierra, reason dice qué campo. Con bulk_send_not_enabled, "
        "esta instancia no tiene el masivo prendido. No confirma, no arranca, no frena y no manda nada."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "template_name": {"type": "string", "description": "El nombre de la plantilla del catálogo."},
            "template_language": {"type": "string",
                                  "description": "Su idioma (es_MX); solo si la plantilla está en más de uno."},
            "template_variables": {
                "type": "array",
                "items": {"anyOf": [
                    {"type": "string"},
                    {"type": "object", "properties": {"de": {"type": "string", "enum": ["nombre"]}},
                     "required": ["de"], "additionalProperties": False},
                ]},
                "description": "Una por marcador del cuerpo, en orden.",
            },
            "header_image_url": {"type": "string", "description": "https de la imagen del encabezado."},
            "scheduled_at": {"type": "string", "description": "La hora central, ISO 8601 con zona."},
            "campaign_label": {"type": "string",
                               "description": "La etiqueta de la campaña que se le pone a cada conversación."},
            "product_refs": {"type": "array", "items": {"type": "string"},
                             "description": "Los productos del envío."},
            "window_mode": {"type": "string", "enum": ["centrada", "desde_la_hora"]},
        },
        "required": ["template_name", "scheduled_at"],
        "additionalProperties": False,
    },
}

GET_BULK_SEND = {
    "name": "get_bulk_send",
    "description": (
        "Relee un masivo: su estado (borrador, confirmado, preparando, enviando, terminado o frenado), la "
        "plantilla y la hora, y los conteos de la lista (cuántas personas sirven, cuántas quedaron afuera y por "
        "qué) y del envío por estado. page_url es el link de su página. Nunca trae un teléfono. Solo lee."
    ),
    "parameters": {
        "type": "object",
        "properties": {"send_id": {**_FLOW_ID, "description": "El id del masivo."}},
        "required": ["send_id"],
        "additionalProperties": False,
    },
}

LIST_BULK_SENDS = {
    "name": "list_bulk_sends",
    "description": (
        "Los masivos de la instancia, del más nuevo al más viejo, con su estado y sus conteos. limit de 1 a 50 "
        "(20 por defecto). Nunca trae un teléfono. Solo lee."
    ),
    "parameters": {
        "type": "object",
        "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50}},
        "additionalProperties": False,
    },
}

# 5.4 (agent-global-pause-v1, D4 y D10 del mapa Z1): pausar al agente comercial. Frenar no manda nada.
PAUSAR_AGENTE = {
    "name": "pausar_agente",
    "description": (
        "Pausa al agente comercial: deja de contestar en todas las conversaciones (alcance todas), en las que "
        "tienen una etiqueta de Chatwoot (alcance etiqueta, valor = la etiqueta) o en las de un producto "
        "(alcance producto, valor = la clave del producto). No manda nada a nadie y no toca los flujos, que "
        "tienen su propia pausa en la página. Lo que entre durante la pausa queda en Chatwoot para el equipo. "
        "Solo a pedido de un aprobador: con not_an_approver, quien te habla no lo es. hasta es opcional (ISO con "
        "zona, entre 5 minutos y 7 días): sin hasta, sigue hasta que alguien la saque. motivo, siempre."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "alcance": {"type": "string", "enum": ["todas", "etiqueta", "producto"]},
            "valor": {"type": "string", "maxLength": 64,
                      "description": "La etiqueta o la clave del producto. Con todas, no va."},
            "hasta": {"type": "string", "maxLength": 40,
                      "description": "Cuándo se levanta sola, ISO 8601 con zona (2026-11-29T21:00:00-06:00)."},
            "motivo": {"type": "string", "maxLength": 500, "description": "Por qué, en una frase."},
        },
        "required": ["alcance", "motivo"],
        "additionalProperties": False,
    },
}

REANUDAR_AGENTE = {
    "name": "reanudar_agente",
    "description": (
        "Saca una pausa del agente (su pause_id sale de estado_pausa). El agente vuelve a contestar desde el "
        "próximo mensaje de cada persona: lo que entró durante la pausa no se contesta para atrás. Solo a "
        "pedido de un aprobador."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pause_id": {"type": "string", "format": "uuid"},
            "motivo": {"type": "string", "maxLength": 500},
        },
        "required": ["pause_id"],
        "additionalProperties": False,
    },
}

ESTADO_PAUSA = {
    "name": "estado_pausa",
    "description": (
        "Si el agente está pausado: las pausas vivas (alcance, hasta, quién la pidió y por qué, cuántas "
        "conversaciones quedaron para el equipo), las últimas levantadas y los productos que se pueden pausar "
        "solos. Solo lee. Con agent_pause_not_enabled, la instancia no lo tiene prendido."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}


# agent-temporary-instruction-v1 (5.3 de Z1): la instruccion temporal para todas las conversaciones.
_INSTRUCTION_TEXT = {
    "type": "string", "minLength": 1, "maxLength": 600,
    "description": ("Lo que el agente tiene que tener en cuenta en todas las conversaciones, en una o dos frases, "
                    "como lo diría el equipo («Hoy no ofrezcas cupón», «Si preguntan por el vivo, se movió al "
                    "jueves 19 h»). Hasta 600 caracteres. Sin links, teléfonos, mails ni montos de dinero: el "
                    "precio y los links salen del conocimiento."),
}
_INSTRUCTION_UNTIL = {
    "type": "string",
    "description": ("Hasta cuándo vale: fecha y hora con zona (por ejemplo 2026-11-28T23:59:00-06:00). "
                    "Obligatorio, en el futuro y a lo sumo 7 días desde ahora. Después el agente la deja de usar solo."),
}

PREVIEW_TEMPORARY_INSTRUCTION = {
    "name": "preview_temporary_instruction",
    "description": (
        "Valida una instrucción temporal y muestra cómo la va a leer el agente (con el encabezado fijo del "
        "producto), sin guardar nada. Vuelve con ok, blocks (qué corregir: link, teléfono o mail, monto, la "
        "fecha) y preview. Con temporary_instruction_not_enabled, esta instancia no la tiene prendida."
    ),
    "parameters": {
        "type": "object",
        "properties": {"texto": _INSTRUCTION_TEXT, "hasta": _INSTRUCTION_UNTIL},
        "required": ["texto", "hasta"],
        "additionalProperties": False,
    },
}

PROPOSE_TEMPORARY_INSTRUCTION = {
    "name": "propose_temporary_instruction",
    "description": (
        "Propone la instrucción temporal. No la publica: el agente no la usa hasta que un aprobador la apruebe en "
        "la página (approval_url, vale 24 horas). Proponer otra reemplaza a la que esperaba el sí. No manda nada a "
        "nadie."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "texto": _INSTRUCTION_TEXT,
            "hasta": _INSTRUCTION_UNTIL,
            "purpose": {"type": "string", "minLength": 1, "maxLength": 500,
                        "description": "Para qué es, en una línea, como lo pidió la persona. Sin teléfonos ni mails."},
        },
        "required": ["texto", "hasta", "purpose"],
        "additionalProperties": False,
    },
}

GET_TEMPORARY_INSTRUCTION = {
    "name": "get_temporary_instruction",
    "description": (
        "Relee una instrucción temporal: su estado (validada, publicada, terminada, retirada, vencida, "
        "descartada), hasta cuándo vale y approval_url (la página para aprobarla o retirarla). Solo lee."
    ),
    "parameters": {
        "type": "object",
        "properties": {"instruction_id": {"type": "string", "format": "uuid"}},
        "required": ["instruction_id"],
        "additionalProperties": False,
    },
}

LIST_TEMPORARY_INSTRUCTIONS = {
    "name": "list_temporary_instructions",
    "description": (
        "Las instrucciones temporales de la instancia, de la más nueva a la más vieja, y cuál está andando "
        "(live_instruction_id). limit de 1 a 50 (10 por defecto). Solo lee. Con temporary_instruction_not_enabled, "
        "esta instancia no la tiene prendida: no la propongas."
    ),
    "parameters": {
        "type": "object",
        "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50}},
        "additionalProperties": False,
    },
}


# 1.3 (contact-export-v1): exportar contactos. El agente pide con filtros; el CSV lo baja un aprobador en la pagina.
EXPORT_CONTACTS = {
    "name": "export_contacts",
    "description": (
        "Pide una lista de contactos de la base en CSV (para el mail, un sorteo, la planilla del asesor) y "
        "devuelve matched_count (cuántas personas cumplen) y page_url: el link de la página donde un aprobador "
        "la baja una sola vez, dentro de los 30 minutos. Mandá el número y page_url tal cual con note: nunca "
        "pegues ni pidas teléfonos, mails o nombres en el chat. Sin filtros salen todas las personas que no "
        "pidieron «No más mensajes» (opted_out_excluded dice cuántas bajas quedaron afuera). Si el pedido no "
        "cierra, reason dice qué campo. Con contact_export_not_enabled, esta instancia no tiene la exportación "
        "prendida. No manda nada a nadie."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "filtros": {
                "type": "object",
                "properties": {
                    "etiquetas": {"type": "array", "items": {"type": "string"}, "maxItems": 10,
                                  "description": "Tiene al menos una de estas etiquetas de Chatwoot."},
                    "sin_etiquetas": {"type": "array", "items": {"type": "string"}, "maxItems": 10,
                                      "description": "No tiene ninguna de estas etiquetas."},
                    "compro": {"type": "boolean", "description": "true: compró; false: no compró."},
                    "producto": {"type": "string",
                                 "description": "Un producto de la instancia: acota compro y el formulario."},
                    "formulario_desde": {"type": "string", "description": "Dejó un formulario desde AAAA-MM-DD."},
                    "formulario_hasta": {"type": "string", "description": "Dejó un formulario hasta AAAA-MM-DD."},
                    "paises": {"type": "array", "items": {"type": "string"}, "maxItems": 10,
                               "description": "El país del teléfono, ISO de dos letras (MX, CO)."},
                    "incluir_bajas": {"type": "boolean",
                                      "description": "Solo si la persona lo pide: incluye las bajas."},
                    "etiquetas_dias": {"type": "integer", "minimum": 1, "maximum": 365,
                                       "description": "Días de conversaciones para las etiquetas (90)."},
                },
                "additionalProperties": False,
            },
            "columnas": {
                "type": "array",
                "items": {"type": "string", "enum": ["nombre", "telefono", "email", "etiquetas", "compro",
                                                     "ultimo_formulario", "pais", "baja", "productos"]},
                "description": "Por defecto nombre, telefono, email, etiquetas, compro y ultimo_formulario.",
            },
        },
        "additionalProperties": False,
    },
}

GET_CONTACT_EXPORT = {
    "name": "get_contact_export",
    "description": (
        "Relee un pedido de exportación: su estado (pendiente, bajado o vencido), cuántas personas "
        "coincidían y, si ya se bajó, cuántas filas. page_url solo mientras está pendiente. Nunca trae un "
        "teléfono, un mail ni un nombre. Solo lee."
    ),
    "parameters": {
        "type": "object",
        "properties": {"export_id": {**_FLOW_ID, "description": "El id del pedido."}},
        "required": ["export_id"],
        "additionalProperties": False,
    },
}
