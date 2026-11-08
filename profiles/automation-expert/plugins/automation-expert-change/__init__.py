"""Registro del plugin de cambios del Automation Expert en Hermes.

El hook que deja pasar solo el mensaje directo 1:1 lo registra el plugin de
lectura (automation-expert-read): este suma solo la guarda del profile para
sus herramientas.
"""

from .approval_button import ACTION_ID, ack_only
from .schemas import (
    ADD_CHATWOOT_LABEL_TO_CONVERSATION,
    CREATE_CHATWOOT_LABEL,
    GET_FLOW_CHANGE,
    GET_FLOW_DEFINITIONS,
    LIST_FLOW_CHANGES,
    LIST_CHATWOOT_LABELS,
    LIST_CHATWOOT_MACROS,
    LIST_TEMPLATES,
    PROPOSE_FLOW_CHANGE,
    VALIDATE_FLOW_CHANGE,
)
from .tools import (
    PROFILE_NAME,
    add_chatwoot_label_to_conversation,
    create_chatwoot_label,
    get_flow_change,
    get_flow_definitions,
    list_chatwoot_labels,
    list_chatwoot_macros,
    list_flow_changes,
    list_templates,
    propose_flow_change,
    validate_flow_change,
)

# template-request-v1 (F-146, parte 5): pedirle una plantilla a Meta. Guardan y validan; ninguna manda.
from .schemas import GET_TEMPLATE_REQUEST, LIST_TEMPLATE_REQUESTS, PROPOSE_TEMPLATE, VALIDATE_TEMPLATE  # noqa: E402
from .tools import get_template_request, list_template_requests, propose_template, validate_template  # noqa: E402

_TEMPLATE_TOOLS = frozenset({"propose_template", "validate_template", "list_template_requests",
                             "get_template_request"})
_TOOLS = frozenset({"get_flow_definitions", "propose_flow_change", "validate_flow_change", "get_flow_change",
                    "list_flow_changes", "list_templates",
                    # automation-expert-chatwoot-v1 (F-143): la herramienta acotada de Chatwoot.
                    "list_chatwoot_labels", "create_chatwoot_label", "add_chatwoot_label_to_conversation",
                    "list_chatwoot_macros"})


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
    ):
        ctx.register_tool(name=name, toolset="automation_expert_change", schema=schema, handler=handler)
    ctx.register_hook("pre_tool_call", make_profile_guard(getattr(ctx, "profile_name", None)))
    # El clic en el boton de URL «Revisar y aprobar»: Slack lo avisa igual, y se reconoce sin hacer nada.
    register_action = getattr(ctx, "register_slack_action_handler", None)
    if callable(register_action):
        register_action(ACTION_ID, ack_only)
