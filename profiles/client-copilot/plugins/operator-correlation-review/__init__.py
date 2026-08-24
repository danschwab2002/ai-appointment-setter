"""Hermes plugin registration for operator correlation review."""

from .schemas import GET_UNRESOLVED_CORRELATION, LIST_UNRESOLVED_CORRELATIONS
from .tools import get_unresolved_correlation, list_unresolved_correlations


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
