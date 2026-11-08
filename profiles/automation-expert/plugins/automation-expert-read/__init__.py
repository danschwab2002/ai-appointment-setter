"""Registro del plugin de lectura del Automation Expert en Hermes."""

from .schemas import DRAW_FLOW, LEAD_HISTORY, LIST_FLOWS, READ_LIBRARY, SKILL_VIEW
from .tools import PROFILE_NAME, draw_flow, lead_history, list_flows, read_library, skill_view

_TOOLS = frozenset({"list_flows", "lead_history", "draw_flow", "skill_view", "read_library"})


def only_one_to_one_dms(**kwargs):
    """pre_gateway_dispatch: en Slack, solo el mensaje directo 1:1.

    allowed_channels deja afuera los canales y los grupos, pero los slash
    commands nativos de Slack no pasan por ese filtro (Hermes 0.21.0,
    plugins/platforms/slack/adapter.py:8448-8606). Un canal de mensaje directo
    1:1 empieza con D; un grupo (mpim) no.
    """
    event = kwargs.get("event")
    source = getattr(event, "source", None)
    # SessionSource.platform es el enum Platform (gateway/session.py:158); su valor es "slack".
    platform = getattr(source, "platform", None)
    if str(getattr(platform, "value", platform) or "").lower() != "slack":
        return None
    chat_id = str(getattr(source, "chat_id", "") or "")
    if chat_id.startswith("D"):
        return None
    return {"action": "skip", "reason": "automation_expert_dm_only"}


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
    ctx.register_tool(
        name="list_flows",
        toolset="automation_expert_read",
        schema=LIST_FLOWS,
        handler=list_flows,
    )
    ctx.register_tool(
        name="lead_history",
        toolset="automation_expert_read",
        schema=LEAD_HISTORY,
        handler=lead_history,
    )
    ctx.register_tool(
        name="draw_flow",
        toolset="automation_expert_read",
        schema=DRAW_FLOW,
        handler=draw_flow,
    )
    # override: reemplaza al skill_view nativo de Hermes (toolset `skills`), con el OK de config.yaml
    # (plugins.entries.automation-expert-read.allow_tool_override). El toolset nativo no se prende: trae
    # tambien skill_manage, que crea y edita skills.
    ctx.register_tool(
        name="skill_view",
        toolset="automation_expert_read",
        schema=SKILL_VIEW,
        handler=skill_view,
        override=True,
    )
    # Solo para las sesiones abiertas antes de E-349: ver tools.read_library.
    ctx.register_tool(
        name="read_library",
        toolset="automation_expert_read",
        schema=READ_LIBRARY,
        handler=read_library,
    )
    ctx.register_hook("pre_gateway_dispatch", only_one_to_one_dms)
    ctx.register_hook("pre_tool_call", make_profile_guard(getattr(ctx, "profile_name", None)))
