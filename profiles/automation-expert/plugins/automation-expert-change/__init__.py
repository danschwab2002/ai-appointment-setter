"""Registro del plugin de cambios del Automation Expert en Hermes.

El hook que deja pasar solo el mensaje directo 1:1 lo registra el plugin de
lectura (automation-expert-read): este suma solo la guarda del profile para
sus herramientas.
"""

from .approval_button import ACTION_ID, ack_only
from .schemas import (
    ADD_CHATWOOT_LABEL_TO_CONVERSATION,
    CREATE_CHATWOOT_LABEL,
    CREATE_CHATWOOT_LABEL_MACRO,
    GET_CAMPAIGN,
    GET_FLOW_CHANGE,
    GET_FLOW_DEFINITIONS,
    LIST_FLOW_CHANGES,
    LIST_CHATWOOT_LABELS,
    LIST_CHATWOOT_MACROS,
    LIST_CAMPAIGNS,
    LIST_TEMPLATES,
    PREVIEW_CAMPAIGN,
    PROPOSE_CAMPAIGN,
    PROPOSE_FLOW_CHANGE,
    VALIDATE_CAMPAIGN,
    VALIDATE_FLOW_CHANGE,
)
from .tools import (
    PROFILE_NAME,
    add_chatwoot_label_to_conversation,
    create_chatwoot_label,
    create_chatwoot_label_macro,
    get_campaign,
    get_flow_change,
    get_flow_definitions,
    list_chatwoot_labels,
    list_campaigns,
    list_chatwoot_macros,
    list_flow_changes,
    list_templates,
    preview_campaign,
    propose_campaign,
    propose_flow_change,
    validate_campaign,
    validate_flow_change,
)

# template-request-v1 (F-146, parte 5): pedirle una plantilla a Meta. Guardan y validan; ninguna manda.
from .schemas import GET_TEMPLATE_REQUEST, LIST_TEMPLATE_REQUESTS, PROPOSE_TEMPLATE, VALIDATE_TEMPLATE  # noqa: E402
from .tools import get_template_request, list_template_requests, propose_template, validate_template  # noqa: E402

# E-465: el link de la pagina de aprobacion, sin propuesta (ver los flujos, pausar, cargar un telefono).
from .schemas import GET_APPROVAL_PAGE  # noqa: E402
from .tools import get_approval_page  # noqa: E402

# E-486 (8d): sumar a alguien al grupo de prueba; el bridge chequea que pida un aprobador.
from .schemas import ADD_TO_TEST_GROUP  # noqa: E402
from .tools import add_to_test_group  # noqa: E402

# keyword-rules-v1 (Z1 3.9): la regla «texto del link -> etiqueta». Publicarla es de la pagina.
from .schemas import GET_KEYWORD_RULE, LIST_KEYWORD_RULES, PREVIEW_KEYWORD_RULE, PROPOSE_KEYWORD_RULE  # noqa: E402
from .tools import get_keyword_rule, list_keyword_rules, preview_keyword_rule, propose_keyword_rule  # noqa: E402

# Encargo 6 del masivo: el borrador y su lectura. Confirmar, arrancar y frenar son de la pagina.
from .schemas import GET_BULK_SEND, LIST_BULK_SENDS, PROPOSE_BULK_SEND  # noqa: E402
from .tools import get_bulk_send, list_bulk_sends, propose_bulk_send  # noqa: E402

# 5.4 (agent-global-pause-v1): pausar al agente comercial; el bridge chequea que pida un aprobador.
from .schemas import ESTADO_PAUSA, PAUSAR_AGENTE, REANUDAR_AGENTE  # noqa: E402
from .tools import estado_pausa, pausar_agente, reanudar_agente  # noqa: E402

# 5.3 de Z1: la instruccion temporal, ver y proponer. Publicarla y retirarla son de la pagina.
from .schemas import (  # noqa: E402
    GET_TEMPORARY_INSTRUCTION,
    LIST_TEMPORARY_INSTRUCTIONS,
    PREVIEW_TEMPORARY_INSTRUCTION,
    PROPOSE_TEMPORARY_INSTRUCTION,
)
from .tools import (  # noqa: E402
    get_temporary_instruction,
    list_temporary_instructions,
    preview_temporary_instruction,
    propose_temporary_instruction,
)

# 1.3 (contact-export-v1): pedir una exportacion y leerla. El CSV lo baja un aprobador en la pagina.
from .schemas import EXPORT_CONTACTS, GET_CONTACT_EXPORT  # noqa: E402
from .tools import export_contacts, get_contact_export  # noqa: E402

# agent-campaign-judge-v1 (4.9): pedir el control de calidad de una campana y leerlo. No publica.
from .schemas import GET_CAMPAIGN_JUDGE, JUDGE_CAMPAIGN  # noqa: E402
from .tools import get_campaign_judge, judge_campaign  # noqa: E402

_TEMPLATE_TOOLS = frozenset({"propose_template", "validate_template", "list_template_requests",
                             "get_template_request"})
_TOOLS = frozenset({"get_flow_definitions", "propose_flow_change", "validate_flow_change", "get_flow_change",
                    "list_flow_changes", "list_templates",
                    # automation-expert-chatwoot-v1 (F-143): la herramienta acotada de Chatwoot.
                    "list_chatwoot_labels", "create_chatwoot_label", "add_chatwoot_label_to_conversation",
                    "list_chatwoot_macros", "create_chatwoot_label_macro",
                    # agent-campaigns-v1 (E-355): armar una campana; publicarla es de la pagina.
                    "preview_campaign", "propose_campaign", "validate_campaign", "get_campaign", "list_campaigns",
                    # E-465: el link de la pagina de aprobacion.
                    "get_approval_page",
                    # E-486: el grupo de prueba, solo agregar y solo un aprobador.
                    "add_to_test_group",
                    # Encargo 6: el masivo, solo el borrador y su lectura.
                    "propose_bulk_send", "get_bulk_send", "list_bulk_sends",
                    # 5.4: la pausa del agente comercial, solo a pedido de un aprobador.
                    "pausar_agente", "reanudar_agente", "estado_pausa",
                    # 5.3 de Z1: la instruccion temporal, solo ver, proponer y leer.
                    "preview_temporary_instruction", "propose_temporary_instruction",
                    "get_temporary_instruction", "list_temporary_instructions",
                    # 1.3: exportar contactos, solo el pedido y su lectura.
                    "export_contacts", "get_contact_export",
                    # keyword-rules-v1 (Z1 3.9): la regla de palabra clave, solo el borrador y su lectura.
                    "preview_keyword_rule", "propose_keyword_rule", "get_keyword_rule", "list_keyword_rules",
                    # 4.9: el control de calidad de una campana, pedirlo y leerlo.
                    "judge_campaign", "get_campaign_judge"})


def make_profile_guard(profile_name):
    """pre_tool_call: las herramientas solo corren en el profile automation-expert."""

    def refuse_outside_profile(**kwargs):
        if kwargs.get("tool_name") in (_TOOLS | _TEMPLATE_TOOLS) and profile_name != PROFILE_NAME:
            return {
                "action": "block",
                "message": "Estas herramientas solo corren en el profile automation-expert.",
            }
        return None

    return refuse_outside_profile


def register(ctx):
    for name, schema, handler in (
        ("get_flow_definitions", GET_FLOW_DEFINITIONS, get_flow_definitions),
        ("propose_flow_change", PROPOSE_FLOW_CHANGE, propose_flow_change),
        ("validate_flow_change", VALIDATE_FLOW_CHANGE, validate_flow_change),
        ("get_flow_change", GET_FLOW_CHANGE, get_flow_change),
        ("list_flow_changes", LIST_FLOW_CHANGES, list_flow_changes),
        ("list_templates", LIST_TEMPLATES, list_templates),
        ("propose_template", PROPOSE_TEMPLATE, propose_template),
        ("validate_template", VALIDATE_TEMPLATE, validate_template),
        ("list_template_requests", LIST_TEMPLATE_REQUESTS, list_template_requests),
        ("get_template_request", GET_TEMPLATE_REQUEST, get_template_request),
        ("list_chatwoot_labels", LIST_CHATWOOT_LABELS, list_chatwoot_labels),
        ("create_chatwoot_label", CREATE_CHATWOOT_LABEL, create_chatwoot_label),
        ("add_chatwoot_label_to_conversation", ADD_CHATWOOT_LABEL_TO_CONVERSATION,
         add_chatwoot_label_to_conversation),
        ("list_chatwoot_macros", LIST_CHATWOOT_MACROS, list_chatwoot_macros),
        ("create_chatwoot_label_macro", CREATE_CHATWOOT_LABEL_MACRO, create_chatwoot_label_macro),
        ("preview_campaign", PREVIEW_CAMPAIGN, preview_campaign),
        ("propose_campaign", PROPOSE_CAMPAIGN, propose_campaign),
        ("validate_campaign", VALIDATE_CAMPAIGN, validate_campaign),
        ("get_campaign", GET_CAMPAIGN, get_campaign),
        ("list_campaigns", LIST_CAMPAIGNS, list_campaigns),
        ("get_approval_page", GET_APPROVAL_PAGE, get_approval_page),
        ("add_to_test_group", ADD_TO_TEST_GROUP, add_to_test_group),
        ("propose_bulk_send", PROPOSE_BULK_SEND, propose_bulk_send),
        ("get_bulk_send", GET_BULK_SEND, get_bulk_send),
        ("list_bulk_sends", LIST_BULK_SENDS, list_bulk_sends),
        ("pausar_agente", PAUSAR_AGENTE, pausar_agente),
        ("reanudar_agente", REANUDAR_AGENTE, reanudar_agente),
        ("estado_pausa", ESTADO_PAUSA, estado_pausa),
        ("preview_temporary_instruction", PREVIEW_TEMPORARY_INSTRUCTION, preview_temporary_instruction),
        ("propose_temporary_instruction", PROPOSE_TEMPORARY_INSTRUCTION, propose_temporary_instruction),
        ("get_temporary_instruction", GET_TEMPORARY_INSTRUCTION, get_temporary_instruction),
        ("list_temporary_instructions", LIST_TEMPORARY_INSTRUCTIONS, list_temporary_instructions),
        ("export_contacts", EXPORT_CONTACTS, export_contacts),
        ("get_contact_export", GET_CONTACT_EXPORT, get_contact_export),
        ("preview_keyword_rule", PREVIEW_KEYWORD_RULE, preview_keyword_rule),
        ("propose_keyword_rule", PROPOSE_KEYWORD_RULE, propose_keyword_rule),
        ("get_keyword_rule", GET_KEYWORD_RULE, get_keyword_rule),
        ("list_keyword_rules", LIST_KEYWORD_RULES, list_keyword_rules),
        ("judge_campaign", JUDGE_CAMPAIGN, judge_campaign),
        ("get_campaign_judge", GET_CAMPAIGN_JUDGE, get_campaign_judge),
    ):
        ctx.register_tool(name=name, toolset="automation_expert_change", schema=schema, handler=handler)
    ctx.register_hook("pre_tool_call", make_profile_guard(getattr(ctx, "profile_name", None)))
    # El clic en el boton de URL «Revisar y aprobar»: Slack lo avisa igual, y se reconoce sin hacer nada.
    register_action = getattr(ctx, "register_slack_action_handler", None)
    if callable(register_action):
        register_action(ACTION_ID, ack_only)
