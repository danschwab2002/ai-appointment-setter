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
from collections.abc import Collection, Mapping
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

# The one URL button a first contact may carry: the dynamic Hotmart checkout,
# whose ``{{1}}`` the sender fills with the issued link (path and query) plus
# ``offDiscount``. It is the same URL the follow-up with a coupon uses
# (``followup_discount.FOLLOWUP_BUTTON_URL``), written here so this module does
# not import the follow-up one.
CHECKOUT_BUTTON_URL = "https://pay.hotmart.com/{{1}}"
# Meta caps the variable part of a URL button at 2000 characters; the link the
# base issues is far shorter. A suffix with whitespace would break the URL.
_BUTTON_SUFFIX_MAX_CHARS = 2000
_BUTTON_SUFFIX_INVALID_RE = re.compile(r"\s")
# The shape of a URL button the caller may declare: an https URL whose only
# variable is one ``{{1}}`` at the end, the dynamic suffix Meta appends. The
# checkout of the cart and the payment slip of OXXO both have it.
_URL_BUTTON_RE = re.compile(r"https://[^\s{}#]+\{\{1\}\}")


def is_static_url(url: object) -> bool:
    """A fixed https URL of a URL button: no ``{{n}}``, so the send fills nothing."""
    return isinstance(url, str) and url.startswith("https://") and "{{" not in url \
        and "}}" not in url and _BUTTON_SUFFIX_INVALID_RE.search(url) is None


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
    # True when the template opens with the URL button the caller declared
    # (the checkout of A1 of the cart, the payment slip of C1): the send must
    # then carry the button suffix, and Chatwoot 4.13 indexes a URL button by
    # its position in ``processed_params.buttons``, which is why the parser
    # only accepts it first. The name stays from A1, the first one.
    has_checkout_button: bool = False
    # The approved URL of that button, ``None`` without one.
    button_url: str | None = None

    def processed_params(
        self,
        values: Mapping[str, str],
        *,
        button_suffix: str | None = None,
    ) -> dict[str, object]:
        """The ``processed_params`` Chatwoot sends to Meta for this template.

        ``values`` are the body variables, checked like ``render``. A template
        with a URL button needs ``button_suffix``; one without refuses
        it, so a suffix never travels to a template that has no place for it.
        """
        self.render(values)
        params: dict[str, object] = {"body": dict(values)}
        if self.has_checkout_button:
            if (
                not isinstance(button_suffix, str)
                or not button_suffix
                or len(button_suffix) > _BUTTON_SUFFIX_MAX_CHARS
                or _BUTTON_SUFFIX_INVALID_RE.search(button_suffix)
            ):
                raise ApprovedTemplateError(
                    REASON_PARAMETERS_MISSING, "invalid_button_suffix"
                )
            params["buttons"] = [{"type": "url", "parameter": button_suffix}]
        elif button_suffix is not None:
            raise ApprovedTemplateError(REASON_MISMATCH, "unexpected_button_suffix")
        return params

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
    expected_category: str | Collection[str],
    parameter_count: int,
    checkout_button: bool = False,
    url_button: str | None = None,
    parameterless_buttons_only: bool = False,
) -> ApprovedTemplate:
    """Pick one template from the inbox catalog and fail closed on any doubt.

    ``checkout_button``: the runtime will send the checkout link in a URL
    button. The template must then open with exactly one URL button on
    ``CHECKOUT_BUTTON_URL`` followed only by quick replies (the cart A1:
    «Usar mi descuento» and «No más mensajes»). Without it, a URL button is
    refused as before (``unsupported_button``).

    ``url_button``: the same, for a URL button the caller declares, exactly as
    Meta approved it (``https://…{{1}}``). The payment slip declares the one of
    the row of its method and country (``boton_prefijo`` + ``{{1}}``), so a
    suffix never lands on another base. ``checkout_button=True`` is
    ``url_button=CHECKOUT_BUTTON_URL``; giving both with another URL is an
    error of the caller.

    ``expected_category``: the category the template must have, or the set of
    categories the caller accepts (the send of a native step: only ``UTILITY``
    with ``permiso: compra``, rule 17; ``MARKETING`` or ``UTILITY`` with any
    other permission, flow-engine-v2 2.6.0). ``ApprovedTemplate.category`` is
    the one of the catalog.

    ``parameterless_buttons_only``: the send of a native step (flow-engine-v2).
    Its buttons may only be the ones the send fills with nothing: URL buttons
    with a fixed URL, without a variable («Completar mi perfil» of the ATT1
    welcome, 26/10), and quick replies («Retomar proceso» and «No mas
    mensajes» of Maribel, 2.6.0), in any order inside the one group of buttons.
    A URL button with a variable is ``url_button_with_variable``; any other
    button is ``unsupported_button``. It excludes ``checkout_button`` and
    ``url_button``.

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
    if url_button is not None and (
        not isinstance(url_button, str) or _URL_BUTTON_RE.fullmatch(url_button) is None
    ):
        raise ValueError("approved_template_url_button_invalid")
    if parameterless_buttons_only and (checkout_button or url_button is not None):
        raise ValueError("approved_template_parameterless_buttons_conflict")
    if isinstance(expected_category, str):
        accepted_categories = frozenset({expected_category.strip()})
    else:
        accepted_categories = frozenset(
            item.strip() for item in expected_category if isinstance(item, str)
        )
    if not accepted_categories or "" in accepted_categories:
        raise ValueError("approved_template_expected_category_invalid")
    if checkout_button:
        if url_button is not None and url_button != CHECKOUT_BUTTON_URL:
            raise ValueError("approved_template_url_button_conflict")
        url_button = CHECKOUT_BUTTON_URL
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
    if not isinstance(category, str) or category.strip() not in accepted_categories:
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

    has_checkout_button = False
    for component in components:
        kind = component.get("type")
        if kind == "BODY":
            continue
        if kind == "BUTTONS":
            buttons = component.get("buttons")
            if not isinstance(buttons, list) or not all(
                isinstance(button, dict) for button in buttons
            ):
                raise ApprovedTemplateError(REASON_MISMATCH, "unsupported_button")
            if parameterless_buttons_only:
                for button in buttons:
                    if button.get("type") == "QUICK_REPLY":
                        continue
                    if button.get("type") != "URL":
                        raise ApprovedTemplateError(REASON_MISMATCH, "unsupported_button")
                    if not is_static_url(button.get("url")):
                        raise ApprovedTemplateError(REASON_MISMATCH, "url_button_with_variable")
                continue
            quick_replies = buttons
            if url_button is not None:
                if has_checkout_button or not buttons:
                    raise ApprovedTemplateError(REASON_MISMATCH, "checkout_button_missing")
                first = buttons[0]
                if first.get("type") != "URL" or first.get("url") != url_button:
                    raise ApprovedTemplateError(REASON_MISMATCH, "checkout_button_not_first")
                has_checkout_button = True
                quick_replies = buttons[1:]
            if not all(button.get("type") == "QUICK_REPLY" for button in quick_replies):
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

    if url_button is not None and not has_checkout_button:
        raise ApprovedTemplateError(REASON_MISMATCH, "checkout_button_missing")

    return ApprovedTemplate(
        name=wanted,
        language=template["language"].strip(),
        category=category.strip(),
        body=body,
        parameter_count=parameter_count,
        has_checkout_button=has_checkout_button,
        button_url=url_button if has_checkout_button else None,
    )
