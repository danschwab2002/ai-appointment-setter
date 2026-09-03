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

PREPARE_CORRELATION_RESOLUTION = {
    "name": "prepare_correlation_resolution",
    "description": (
        "Prepare an expiring manual resolution command after the human operator "
        "has explicitly chosen a listed candidate or explicitly chosen to close "
        "without a match. Never choose an action or candidate autonomously. This "
        "step does not apply the resolution and keeps automation blocked."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "case_id": {"type": "string", "format": "uuid"},
            "idempotency_key": {
                "type": "string",
                "format": "uuid",
                "description": "Fresh UUID for a new decision; reuse it for an exact retry.",
            },
            "action": {
                "type": "string",
                "enum": ["resolve_with_candidate", "close_without_match"],
            },
            "candidate_id": {
                "type": ["string", "null"],
                "format": "uuid",
                "description": (
                    "Exact candidate returned by get_unresolved_correlation; null "
                    "only for close_without_match."
                ),
            },
            "verification_basis": {
                "type": "string",
                "enum": [
                    "external_transaction_reference",
                    "operator_source_record",
                    "customer_confirmation",
                    "no_valid_candidate_after_review",
                ],
            },
        },
        "required": [
            "case_id",
            "idempotency_key",
            "action",
            "candidate_id",
            "verification_basis",
        ],
        "additionalProperties": False,
    },
}

CONFIRM_CORRELATION_RESOLUTION = {
    "name": "confirm_correlation_resolution",
    "description": (
        "Apply one exact prepared command. Call only after showing the preview and "
        "receiving a new explicit human confirmation. Hermes always presents a "
        "native human approval gate before this tool can execute."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command_id": {"type": "string", "format": "uuid"},
            "expected_action": {
                "type": "string",
                "enum": ["resolve_with_candidate", "close_without_match"],
            },
            "expected_candidate_id": {
                "type": ["string", "null"],
                "format": "uuid",
            },
        },
        "required": [
            "command_id",
            "expected_action",
            "expected_candidate_id",
        ],
        "additionalProperties": False,
    },
}
