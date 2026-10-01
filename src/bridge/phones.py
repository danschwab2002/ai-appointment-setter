"""WhatsApp phone equivalence between sources, for the portable runtime.

The same mobile reaches the bridge in two forms. The landing form (the GHL
adapter and ``/webhooks/lead``) stores a Mexican number as ``52`` + 10 digits
and an Argentine one as ``54`` + 10; Hotmart and the WhatsApp ``wa_id`` bring
``521`` + 10 and ``549`` + 10. Nothing is rewritten when it is stored: every
source keeps validating what it saved against its own raw payload. The two
forms are only *compared* in one canonical form.

This module is the Python mirror of ``_whatsapp_phone_canonical`` and
``_whatsapp_phone_variants`` (migration ``20261001000100``). The rule is
anchored by length: only 13 digits starting with ``521`` or ``549`` are
rewritten, so a 10-digit national number that starts with 1 or 9 (12 digits
in total) stays equal to itself. Brazil (the ninth digit) is out on purpose:
there is no measurement.

Only code that runs with an instance manifest uses it. A runtime without one
(Johanna) keeps comparing phones exactly.
"""

from __future__ import annotations

import re

_NON_DIGIT = re.compile(r"[^0-9]")
_MX_WHATSAPP_FORM = re.compile(r"521[0-9]{10}")
_AR_WHATSAPP_FORM = re.compile(r"549[0-9]{10}")
_MX_CANONICAL_FORM = re.compile(r"52[0-9]{10}")
_AR_CANONICAL_FORM = re.compile(r"54[0-9]{10}")


def canonical_whatsapp_phone(phone: str | None) -> str | None:
    """Digits only, with ``521``/``549`` + 10 rewritten to ``52``/``54`` + 10.

    ``None`` when the value carries no digit at all.
    """
    if not isinstance(phone, str):
        return None
    digits = _NON_DIGIT.sub("", phone)
    if not digits:
        return None
    if _MX_WHATSAPP_FORM.fullmatch(digits) is not None:
        return "52" + digits[3:]
    if _AR_WHATSAPP_FORM.fullmatch(digits) is not None:
        return "54" + digits[3:]
    return digits


def equivalent_whatsapp_phones(phone: str | None) -> tuple[str, ...]:
    """The forms that share one canonical phone: the canonical one first.

    Two for a Mexican or Argentine mobile (``52…`` then ``521…``; ``54…`` then
    ``549…``), one for any other number, none without digits. The second form
    is the one WhatsApp reports as ``wa_id``.
    """
    canonical = canonical_whatsapp_phone(phone)
    if canonical is None:
        return ()
    if _MX_CANONICAL_FORM.fullmatch(canonical) is not None:
        return (canonical, "521" + canonical[2:])
    if _AR_CANONICAL_FORM.fullmatch(canonical) is not None:
        return (canonical, "549" + canonical[2:])
    return (canonical,)


def same_whatsapp_phone(first: str | None, second: str | None) -> bool:
    """True when both values are the same phone in canonical form."""
    canonical = canonical_whatsapp_phone(first)
    return canonical is not None and canonical == canonical_whatsapp_phone(second)


def whatsapp_delivery_phone(phone: str | None) -> str | None:
    """The form used to send and to create the Chatwoot contact (D13).

    This is the only place where that decision lives. Measured on the
    production Chatwoot (4.13) on 2026-10-01:

    * Mexico: every contact that wrote in has the ``wa_id`` with the 1
      (``521`` + 10), and Chatwoot has no normalizer for Mexico, so a reply
      always lands on the ``521…`` contact. A contact created as ``52`` + 10
      gets its reply in another conversation. The delivery form is ``521`` + 10.
    * Argentina: Chatwoot looks for a ``54…`` contact (without the 9) before
      using the incoming ``549…``, and a template sent to ``54`` + 10 was
      delivered with the reply in the same conversation. The delivery form
      stays ``54`` + 10.
    * Any other number is sent as it is.

    Not yet proven on an ATT1 conversation: that is the E2E before a flow is
    turned on.
    """
    canonical = canonical_whatsapp_phone(phone)
    if canonical is None:
        return None
    if _MX_CANONICAL_FORM.fullmatch(canonical) is not None:
        return "521" + canonical[2:]
    return canonical


def whatsapp_phone_region(phone: str | None) -> str:
    """``MX``, ``AR`` or ``other``: what a log may say instead of the number."""
    canonical = canonical_whatsapp_phone(phone)
    if canonical is None:
        return "other"
    if _MX_CANONICAL_FORM.fullmatch(canonical) is not None:
        return "MX"
    if _AR_CANONICAL_FORM.fullmatch(canonical) is not None:
        return "AR"
    return "other"
