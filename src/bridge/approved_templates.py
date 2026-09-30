"""The approved Meta template the durable dispatcher sends without Hermes.

In a WABA first contact the only text Meta shows the lead is the approved body
of the template with its variables filled. A draft from Hermes never reaches
Meta: it only ended up in the ``content`` Chatwoot stores, and the shared SOUL
forbids the agent from writing first. So the direct mode reads the approved
body from the catalog Chatwoot publishes for the inbox, fills it with the same
values the sender puts in ``processed_params``, and hands that text to the
final Meta gate and to Chatwoot.

It follows the pattern of ``reactivation.parse_reactivation_template`` without
touching it: the catalog is read on every send, so a template that Meta pauses
or rejects stops the send instead of producing messages Meta refuses.

See ``docs/contracts/approved-template-direct-dispatch-v1.md``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

# Meta caps the body of a template at 1024 characters, variables included. The
# general validator of an agent draft stops at 500, which a rendered approved
# body can exceed, so the direct mode checks its own limit.
APPROVED_TEMPLATE_BODY_MAX_CHARS = 1024

# The stable reasons the dispatcher records as the ``reason_code`` of the
# attempt. ``detail`` in the exception says which check failed, for the log.
REASON_UNAVAILABLE = "approved_template_unavailable"
REASON_MISMATCH = "approved_template_mismatch"
REASON_PARAMETERS_MISSING = "template_parameters_missing"

_PLACEHOLDER_RE = re.compile(r"\{\{(\d+)\}\}")
# Meta refuses a body parameter with a new line, a tab or more than four
# consecutive spaces (error 132018), after the request has started. The values
# the bridge builds are already collapsed (messaging.WhatsAppTemplateConfig.
# body_values); render() refuses anything else before the final gate.
_META_INVALID_PARAMETER_RE = re.compile(r"[\n\r\t]| {5,}")
# Components the send can carry with only body parameters. A static header or
# footer needs nothing; a quick reply button needs no parameter either.
_STATIC_COMPONENTS = frozenset({"HEADER", "FOOTER"})


class ApprovedTemplateError(Exception):
    """The catalog or the values do not allow sending this template."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}:{detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class ApprovedTemplate:
    """An APPROVED template whose body has exactly ``{{1}}`` to ``{{n}}``."""

    name: str
    language: str
    category: str
    body: str
    parameter_count: int

    def render(self, values: Mapping[str, str]) -> str:
        """The text Meta will show: the approved body with its variables filled.

        ``values`` must carry exactly ``"1"`` to ``"n"``, none empty and none
        with a new line, a tab or more than four consecutive spaces: Meta
        rejects the send for any of them. The substitution is one pass, so a
        value that contains ``{{2}}`` is not substituted again.
        """
        expected = {str(position) for position in range(1, self.parameter_count + 1)}
        if set(values) != expected:
            raise ApprovedTemplateError(REASON_MISMATCH, "parameter_count_mismatch")
        for value in values.values():
            if not isinstance(value, str) or not value.strip():
                raise ApprovedTemplateError(REASON_PARAMETERS_MISSING, "empty_parameter")
            if _META_INVALID_PARAMETER_RE.search(value):
                raise ApprovedTemplateError(
                    REASON_PARAMETERS_MISSING, "invalid_parameter_whitespace"
                )
        rendered = _PLACEHOLDER_RE.sub(lambda match: values[match.group(1)], self.body)
        if len(rendered) > APPROVED_TEMPLATE_BODY_MAX_CHARS:
            raise ApprovedTemplateError(REASON_MISMATCH, "rendered_body_too_long")
        return rendered


def parse_approved_template(
    inbox_payload: object,
    *,
    template_name: str,
    expected_language: str,
    expected_category: str,
    parameter_count: int,
) -> ApprovedTemplate:
    """Pick one template from the inbox catalog and fail closed on any doubt.

    ``approved_template_unavailable``: the catalog is unreadable, the template
    is not there, or it is not APPROVED. ``approved_template_mismatch``: it is
    there but does not match what the runtime would send (language, category,
    placeholders, a component that needs parameters the bridge does not send).
    """
    if not isinstance(template_name, str) or not template_name.strip():
        raise ValueError("approved_template_name_missing")
    if not isinstance(parameter_count, int) or isinstance(parameter_count, bool) or (
        parameter_count < 1
    ):
        raise ValueError("approved_template_parameter_count_invalid")
    if not isinstance(inbox_payload, dict):
        raise ApprovedTemplateError(REASON_UNAVAILABLE, "invalid_inbox_payload")
    templates = inbox_payload.get("message_templates")
    if not isinstance(templates, list):
        raise ApprovedTemplateError(REASON_UNAVAILABLE, "invalid_inbox_payload")

    wanted = template_name.strip()
    named = [
        template
        for template in templates
        if isinstance(template, dict) and template.get("name") == wanted
    ]
    if not named:
        raise ApprovedTemplateError(REASON_UNAVAILABLE, "not_found")
    # Meta keys a template by name and language: the same name can exist in
    # several languages. The runtime sends one language.
    same_language = [
        template
        for template in named
        if isinstance(template.get("language"), str)
        and template["language"].strip() == expected_language.strip()
    ]
    if not same_language:
        raise ApprovedTemplateError(REASON_MISMATCH, "language_mismatch")
    if len(same_language) != 1:
        raise ApprovedTemplateError(REASON_MISMATCH, "ambiguous_template")
    template = same_language[0]

    if template.get("status") != "APPROVED":
        raise ApprovedTemplateError(REASON_UNAVAILABLE, "not_approved")
    category = template.get("category")
    if not isinstance(category, str) or category.strip() != expected_category.strip():
        raise ApprovedTemplateError(REASON_MISMATCH, "category_mismatch")

    components = template.get("components")
    if not isinstance(components, list) or not all(
        isinstance(component, dict) for component in components
    ):
        raise ApprovedTemplateError(REASON_MISMATCH, "invalid_components")
    bodies = [component for component in components if component.get("type") == "BODY"]
    if len(bodies) != 1:
        raise ApprovedTemplateError(REASON_MISMATCH, "invalid_body")
    body = bodies[0].get("text")
    if not isinstance(body, str) or not body.strip():
        raise ApprovedTemplateError(REASON_MISMATCH, "invalid_body")
    # Exactly {{1}}..{{n}}. One too many and Meta rejects the send for a missing
    # parameter; one too few and a declared value goes nowhere. A named
    # placeholder ({{first_name}}) is left over after removing the positional
    # ones, and the bridge only sends positional values.
    placeholders = sorted({int(number) for number in _PLACEHOLDER_RE.findall(body)})
    if placeholders != list(range(1, parameter_count + 1)):
        raise ApprovedTemplateError(REASON_MISMATCH, "unexpected_placeholders")
    if "{{" in _PLACEHOLDER_RE.sub("", body):
        raise ApprovedTemplateError(REASON_MISMATCH, "unexpected_placeholders")

    for component in components:
        kind = component.get("type")
        if kind == "BODY":
            continue
        if kind == "BUTTONS":
            buttons = component.get("buttons")
            if not isinstance(buttons, list) or not all(
                isinstance(button, dict) and button.get("type") == "QUICK_REPLY"
                for button in buttons
            ):
                raise ApprovedTemplateError(REASON_MISMATCH, "unsupported_button")
            continue
        if kind in _STATIC_COMPONENTS:
            text = component.get("text")
            if (kind == "HEADER" and component.get("format", "TEXT") != "TEXT") or (
                isinstance(text, str) and "{{" in text
            ):
                raise ApprovedTemplateError(
                    REASON_MISMATCH, "component_requires_parameters"
                )
            continue
        raise ApprovedTemplateError(REASON_MISMATCH, "unsupported_component")

    return ApprovedTemplate(
        name=wanted,
        language=template["language"].strip(),
        category=category.strip(),
        body=body,
        parameter_count=parameter_count,
    )
