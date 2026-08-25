"""Hermes plugin registration for operator correlation review."""

from .schemas import (
    CONFIRM_CORRELATION_RESOLUTION,
    GET_UNRESOLVED_CORRELATION,
    LIST_UNRESOLVED_CORRELATIONS,
    PREPARE_CORRELATION_RESOLUTION,
)
from .tools import (
    confirm_correlation_resolution,
    get_unresolved_correlation,
    list_unresolved_correlations,
    prepare_correlation_resolution,
    require_resolution_confirmation,
)


def register(ctx):
    ctx.register_tool(
        name="list_unresolved_correlations",
        toolset="operator_correlation_review",
        schema=LIST_UNRESOLVED_CORRELATIONS,
        handler=list_unresolved_correlations,
    )
    ctx.register_tool(
        name="get_unresolved_correlation",
        toolset="operator_correlation_review",
        schema=GET_UNRESOLVED_CORRELATION,
        handler=get_unresolved_correlation,
    )
    ctx.register_tool(
        name="prepare_correlation_resolution",
        toolset="operator_correlation_review",
        schema=PREPARE_CORRELATION_RESOLUTION,
        handler=prepare_correlation_resolution,
    )
    ctx.register_tool(
        name="confirm_correlation_resolution",
        toolset="operator_correlation_review",
        schema=CONFIRM_CORRELATION_RESOLUTION,
        handler=confirm_correlation_resolution,
    )
    ctx.register_hook("pre_tool_call", require_resolution_confirmation)
