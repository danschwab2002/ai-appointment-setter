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
from typing import NamedTuple

import phonenumbers

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

    It is also the form that picks the contact when Chatwoot already has the
    same mobile under both (D14): the reply lands where Chatwoot resolves the
    incoming ``wa_id``, and that is this form.

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


# --- bulk lists ------------------------------------------------------------------------------
#
# A bulk send imports a list (a GHL export, a spreadsheet) where the phones come
# written by people: with or without ``+``, with ``00``, with spaces, with the
# Mexican ``1`` or the Argentine ``9``, or without a country code at all. The
# rules below are the ones of the 2026-11-22 Black Pack send in Maribel
# (``normalizar_tel`` of the ``masivo.py`` kit, which ran in production), and
# they end in ``whatsapp_delivery_phone`` so a reply lands in the same
# conversation as every other send. "Verified" means the number has the shape
# of a mobile of its country; whether it has WhatsApp is only known when Meta
# answers the send (131026).

BULK_PHONE_OK = "ok"
BULK_PHONE_REASONS = (
    "vacio",  # nothing written
    "sin_digitos",  # text without a single digit
    "sin_pais",  # no country code and not a national number of the instance
    "co_no_movil",  # Colombia, but not 10 digits starting with 3
    "mx_largo",  # Mexico, but not 10 national digits (with or without the 1)
    "ar_largo",  # Argentina, but not 10 national digits (with or without the 9)
    "largo",  # any other country, fewer than 8 or more than 15 digits
    "no_movil",  # phonenumbers says it is a valid number that is not a mobile
)
# The country codes accepted without a ``+``: the ones of the instances and of
# their buyers (Hotmart). A number without ``+`` that starts with one of them is
# taken as international; anything else needs the instance's country.
_BULK_KNOWN_INTERNATIONAL = re.compile(
    r"(57|52|54|1|51|56|58|591|593|595|598|502|503|504|505|506|507|34)[0-9]{7,12}"
)
# phonenumbers types that are certainly not a WhatsApp mobile. FIXED_LINE_OR_MOBILE
# (Mexico, the US) and VOIP stay in: the number may have WhatsApp.
_BULK_NOT_MOBILE_TYPES = frozenset(
    {
        phonenumbers.PhoneNumberType.FIXED_LINE,
        phonenumbers.PhoneNumberType.TOLL_FREE,
        phonenumbers.PhoneNumberType.PREMIUM_RATE,
        phonenumbers.PhoneNumberType.SHARED_COST,
        phonenumbers.PhoneNumberType.PAGER,
        phonenumbers.PhoneNumberType.UAN,
        phonenumbers.PhoneNumberType.VOICEMAIL,
    }
)


class BulkPhone(NamedTuple):
    """The delivery form (digits, as ``whatsapp_delivery_phone``) or ``None``,
    and ``"ok"`` or the reason it was left out (one of ``BULK_PHONE_REASONS``)."""

    phone: str | None
    reason: str


def normalize_bulk_phone(raw: object, country_code: str | None) -> BulkPhone:
    """Normalize a phone written in a bulk list, for the instance's country.

    ``country_code`` is the instance's calling code without ``+`` (``"57"``,
    ``"52"``…): a national number of 10 digits gets it. Without it, only a
    number that already carries its code is accepted.

    * Colombia: ``57`` + 10 digits starting with 3 (a ``6…`` is a landline).
      With the instance in Colombia, 10 digits starting with 3 get the ``57``.
    * Mexico: ``52`` + 10, with or without the ``1``; sent as ``521`` + 10.
    * Argentina: ``54`` + 10, with or without the ``9``; sent as ``54`` + 10.
    * Any other country: 8 to 15 digits, sent as they are.

    Then ``phonenumbers`` gives a second opinion: a number it knows as valid
    and of a type that is not a mobile (a landline, a toll-free) is left out as
    ``no_movil``. A number it does not know is not left out for that: its
    metadata may lag behind a new range, and Meta says the last word.
    """
    text = "" if raw is None else str(raw).strip()
    if not text:
        return BulkPhone(None, "vacio")
    international = text.startswith("+")
    digits = _NON_DIGIT.sub("", text)
    if digits.startswith("00"):
        digits, international = digits[2:], True
    if not digits:
        return BulkPhone(None, "sin_digitos")
    country = (country_code or "").strip().lstrip("+")
    if not international:
        if len(digits) == 10 and digits.startswith("3") and country == "57":
            digits = "57" + digits
        elif len(digits) == 10 and country and country != "57":
            digits = country + digits
        elif _BULK_KNOWN_INTERNATIONAL.fullmatch(digits) is None:
            return BulkPhone(None, "sin_pais")
    if digits.startswith("57"):
        national = digits[2:]
        if len(national) != 10 or not national.startswith("3"):
            return BulkPhone(None, "co_no_movil")
        dialed = digits
    elif digits.startswith("52"):
        national = digits[2:]
        if len(national) == 11 and national.startswith("1"):
            national = national[1:]
        if len(national) != 10:
            return BulkPhone(None, "mx_largo")
        digits, dialed = "52" + national, "52" + national
    elif digits.startswith("54"):
        national = digits[2:]
        if len(national) == 11 and national.startswith("9"):
            national = national[1:]
        if len(national) != 10:
            return BulkPhone(None, "ar_largo")
        # phonenumbers only knows an Argentine mobile with the 9.
        digits, dialed = "54" + national, "549" + national
    elif not 8 <= len(digits) <= 15:
        return BulkPhone(None, "largo")
    else:
        dialed = digits
    if _bulk_not_a_mobile(dialed):
        return BulkPhone(None, "no_movil")
    delivery = whatsapp_delivery_phone(digits)
    if delivery is None:  # pragma: no cover - digits is never empty here
        return BulkPhone(None, "sin_digitos")
    return BulkPhone(delivery, BULK_PHONE_OK)


def _bulk_not_a_mobile(digits: str) -> bool:
    try:
        number = phonenumbers.parse("+" + digits, None)
    except phonenumbers.NumberParseException:
        return False
    if not phonenumbers.is_valid_number(number):
        return False
    return phonenumbers.number_type(number) in _BULK_NOT_MOBILE_TYPES
