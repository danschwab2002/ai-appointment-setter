"""Schemas exposed by the operator correlation review plugin."""

LIST_UNRESOLVED_CORRELATIONS = {
    "name": "list_unresolved_correlations",
    "description": (
        "List deterministic Hotmart purchase-intent correlations that remain "
        "unmatched, ambiguous, or conflicting and require human review. Use when "
        "the operator asks which correlation cases are pending. This tool is read-only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "default": 20,
                "description": "Maximum number of newest pending cases to return.",
            }
        },
        "additionalProperties": False,
    },
}

GET_UNRESOLVED_CORRELATION = {
    "name": "get_unresolved_correlation",
    "description": (
        "Get the masked evidence and candidates for one exact unresolved "
        "correlation case. Use only with a case_id returned by the list tool. "
        "This tool is read-only and cannot resolve the case."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "case_id": {
                "type": "string",
                "format": "uuid",
                "description": "Exact unresolved correlation case ID.",
            }
        },
        "required": ["case_id"],
        "additionalProperties": False,
    },
}
