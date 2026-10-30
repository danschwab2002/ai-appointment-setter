"""Registro del plugin de cambios del Automation Expert en Hermes.

El hook que deja pasar solo el mensaje directo 1:1 lo registra el plugin de
lectura (automation-expert-read): este suma solo la guarda del profile para
sus herramientas.
"""

from .approval_button import ACTION_ID, ack_only
from .schemas import (
    GET_FLOW_CHANGE,
    GET_FLOW_DEFINITIONS,
    LIST_FLOW_CHANGES,
    LIST_TEMPLATES,
    PROPOSE_FLOW_CHANGE,
    VALIDATE_FLOW_CHANGE,
)
from .tools import (
    PROFILE_NAME,
    get_flow_change,
    get_flow_definitions,
    list_flow_changes,
    list_templates,
    propose_flow_change,
    validate_flow_change,
)

_TOOLS = frozenset({"get_flow_definitions", "propose_flow_change", "validate_flow_change", "get_flow_change",
                    "list_flow_changes", "list_templates"})


def make_profile_guard(profile_name):
    """pre_tool_call: las herramientas solo corren en el profile automation-expert."""

    def refuse_outside_profile(**kwargs):
        if kwargs.get("tool_name") in _TOOLS and profile_name != PROFILE_NAME:
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
    ):
        ctx.register_tool(name=name, toolset="automation_expert_change", schema=schema, handler=handler)
    ctx.register_hook("pre_tool_call", make_profile_guard(getattr(ctx, "profile_name", None)))
    # El clic en el boton de URL «Revisar y aprobar»: Slack lo avisa igual, y se reconoce sin hacer nada.
    register_action = getattr(ctx, "register_slack_action_handler", None)
    if callable(register_action):
        register_action(ACTION_ID, ack_only)
