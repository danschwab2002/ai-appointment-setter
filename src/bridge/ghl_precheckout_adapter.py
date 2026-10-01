"""Translate the GHL form-submission webhook into ``lead.precheckout`` 1.1.0.

Contract: ``docs/contracts/ghl-precheckout-adapter-v1.md``. Pure: no network, no
database and no import of ``app``. The HTTP route authenticates, calls
:func:`parse_ghl_body` and :func:`translate_ghl_form_submission`, and admits the
translated event through ``parse_lead_precheckout`` and the portable admission,
the same path as a landing form in a runtime with a manifest.

What is read from the GHL body, and only this: ``contact_id``, ``full_name`` (or
``first_name`` + ``last_name``), ``email``, ``phone``, the top-level
``attributionSource`` (the submission that fired the workflow) and
``customData.setter_token``. Never the first or last touch under ``contact``, the
structured UTM fields, ``fbEventId``, ``tags``, ``country`` or the custom fields
that GHL puts as top-level keys.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from urllib.parse import parse_qs, urlsplit

import phonenumbers

from .checkout_issuance import generate_issuance_ulid
from .commercial_ally import CommercialAllyConfig, OfferLanding

GHL_SETTER_TOKEN_FIELD = "setter_token"

# reason -> HTTP status. The route answers ``ghl_<reason>``.
REJECTION_STATUS: Mapping[str, int] = MappingProxyType(
    {
        "invalid_json": 400,
        "invalid_payload": 400,
        "not_a_form_submission": 422,
        "form_not_allowed": 422,
        "landing_unknown": 422,
        "landing_ambiguous": 422,
        "phone_unusable": 422,
    }
)

# The same edge rules as ``lead_precheckout`` (the parser checks them again).
_ASCII_TRIM_CHARS = " \t\n\r\f\v"
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_EMAIL_MAX_LENGTH = 254  # RFC 5321: the longest address a mail server accepts
_REGION = re.compile(r"[A-Z]{2}")
# identity.phone of the portable admission (migration 20260930000200) and the
# check of purchase_intents.normalized_phone.
_RPC_PHONE_DIGITS = re.compile(r"[1-9][0-9]{7,14}")
# Mexico stopped dialing the ``1`` of mobiles in 2019 and phonenumbers rejects it.
_MX_LEGACY_MOBILE = re.compile(r"\+521([0-9]{10})")
_UTM_FIELDS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# What no admission can carry: U+0000 is refused by a jsonb text (22P05) and a lone
# surrogate cannot be encoded as UTF-8. The parser lets both through, so an event
# with one fails the RPC on every delivery: the route would answer 503 and GHL
# would retry forever. Anyone can put one in the landing URL (``%00``).
_UNSTORABLE = re.compile("[\u0000\ud800-\udfff]")

# ---------------------------------------------------------------- sck (core port)
# Literal port of ``readSck``, ``sanearCampoSck`` and ``sanearSckEntrante`` from
# lancemos/core ``src/superpowers/client.js`` (4ec04a7, standards E01, E02, E10 and
# E13), so the agent's link keeps the label the landing would have composed.
_SCK_SEPARATOR = "~"
_SCK_FIELDS = ("source", "term", "content", "medium", "campaign")
_SCK_OPTIONAL_FIELD = "id"
_SCK_ALPHABET = re.compile(r"[^A-Za-z0-9._~-]")
_COMBINING_MARKS = re.compile("[̀-ͯ]")
_CONTROL_CHARS = re.compile("[\u0000-\u001f\u007f]")
# JavaScript's ``\s`` (WhiteSpace and LineTerminator), not Python's: Python also
# counts U+001C-U+001F and U+0085 and leaves out U+FEFF.
_JS_WHITESPACE_RUN = re.compile(
    "[\t\n\u000b\u000c\r    -     　﻿]+"
)
_DASH_RUN = re.compile(r"-{2,}")


class GhlAdapterRejection(ValueError):
    """The GHL body cannot become a ``lead.precheckout``; nothing reaches the base.

    ``str()`` is only the reason: never a value from the body.
    ``phone_region`` is set on ``phone_unusable`` when the country code is
    readable, so the route can log the region without the number.
    """

    def __init__(
        self, reason: str, status_code: int, *, phone_region: str | None = None
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code
        self.phone_region = phone_region


def _reject(reason: str, *, phone_region: str | None = None) -> GhlAdapterRejection:
    return GhlAdapterRejection(
        reason, REJECTION_STATUS[reason], phone_region=phone_region
    )


@dataclass(frozen=True)
class GhlTranslation:
    event: dict[str, object]  # lead.precheckout 1.1.0, ready for parse_lead_precheckout
    form_id: str
    site: str
    landing_id: str
    offer_code: str
    phone_region: str
    has_utm: bool
    has_fbclid: bool


class _DuplicateKey(ValueError):
    pass


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise _DuplicateKey
    return obj


def _reject_non_json_constant(name: str) -> object:
    raise ValueError(f"{name} is not JSON")


def parse_ghl_body(raw: bytes) -> dict[str, object]:
    """The GHL body as a JSON object, refusing a repeated key at any level.

    GHL puts the custom fields as top-level keys named after their visible label,
    in the same namespace as ``email``, ``phone`` and ``contact_id``; ``json.loads``
    alone would keep the last one silently. Raises ``invalid_json`` (not JSON:
    syntax, encoding, ``NaN``/``Infinity``, nesting too deep) or
    ``invalid_payload`` (a repeated key, or not an object), both 400.
    """

    try:
        body = json.loads(
            raw,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_non_json_constant,
        )
    except _DuplicateKey:
        raise _reject("invalid_payload") from None
    except (ValueError, RecursionError):
        # JSONDecodeError and UnicodeDecodeError are ValueError.
        raise _reject("invalid_json") from None
    if not isinstance(body, dict):
        raise _reject("invalid_payload")
    return body


def ghl_body_token(body: Mapping[str, object]) -> str | None:
    """``customData.setter_token``, the token the standard Webhook action can send.

    ``None`` when the body carries no token. A present value that is not text
    comes back as ``""``, which never matches a configured token (32 characters or
    more): a malformed token fails closed instead of counting as absent.
    """

    custom = body.get("customData")
    if not isinstance(custom, Mapping) or GHL_SETTER_TOKEN_FIELD not in custom:
        return None
    token = custom[GHL_SETTER_TOKEN_FIELD]
    return token if isinstance(token, str) else ""


def _search_params(query: str) -> dict[str, list[str]]:
    # ``new URLSearchParams(location.search)``: drops one leading ``?``, splits on
    # ``&``, ``+`` is a space, percent-decoding as UTF-8 with replacement.
    if query.startswith("?"):
        query = query[1:]
    return parse_qs(query, keep_blank_values=True)


def _first(params: Mapping[str, list[str]], name: str) -> str:
    values = params.get(name)
    return values[0] if values else ""


def _sanitize_sck_field(value: str) -> str:
    """``sanearCampoSck``: one field of the composed ``sck``."""

    value = unicodedata.normalize("NFD", value)
    value = _COMBINING_MARKS.sub("", value)
    value = _CONTROL_CHARS.sub("", value)
    value = "-".join(value.split(_SCK_SEPARATOR))
    value = _JS_WHITESPACE_RUN.sub("-", value)
    value = _SCK_ALPHABET.sub("", value)
    value = _DASH_RUN.sub("-", value)
    return value.strip("-")


def _sanitize_incoming_sck(value: str) -> str:
    """``sanearSckEntrante``: the ``sck`` written in the ad URL, barely touched."""

    value = _CONTROL_CHARS.sub("", value)
    value = _JS_WHITESPACE_RUN.sub(" ", value)
    # After the collapse the only whitespace left at the edges is a space, so this
    # is JavaScript's ``trim()``.
    return value.strip(" ")


def compose_lancemos_sck(query: str) -> str:
    """The ``sck`` the Lancemos core composes from a URL query (``readSck``).

    ``query`` is the query of the page URL, with or without its ``?``.

    - Any UTM (``utm_id`` included, judged after sanitizing): the five fields
      ``source~term~content~medium~campaign`` with empty positions kept, and
      ``~id`` at the end only when it came.
    - No UTM: the ``sck`` of the URL, with control characters removed and spaces
      collapsed.
    - Neither: ``""``. Attribution is never invented.

    It does not check the alphabet nor the length: the link emission drops an
    ``sck`` outside ``[A-Za-z0-9._|~-]{1,255}`` and records it, as for a landing.
    """

    params = _search_params(query)
    values = [_sanitize_sck_field(_first(params, f"utm_{field}")) for field in _SCK_FIELDS]
    campaign_id = _sanitize_sck_field(_first(params, f"utm_{_SCK_OPTIONAL_FIELD}"))
    if campaign_id:
        values.append(campaign_id)
    if any(values):
        return _SCK_SEPARATOR.join(values)
    return _sanitize_incoming_sck(_first(params, "sck"))


# ------------------------------------------------------------------ translation


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip(_ASCII_TRIM_CHARS)
    return cleaned or None


def _storable(value: str) -> str:
    """``value`` without what no admission can carry (``_UNSTORABLE``)."""

    return _UNSTORABLE.sub("", value)


def _name_part(value: object) -> str | None:
    # The name is display text: an invisible NUL or a lone surrogate is dropped
    # instead of losing the lead.
    return _text(_storable(value)) if isinstance(value, str) else None


def _buyer_name(body: Mapping[str, object]) -> str | None:
    full_name = _name_part(body.get("full_name"))
    if full_name is not None:
        return full_name
    parts = (_name_part(body.get("first_name")), _name_part(body.get("last_name")))
    return " ".join(part for part in parts if part) or None


def _without_trailing_slash(path: str) -> str:
    return path[:-1] if len(path) > 1 and path.endswith("/") else path


def _offer_landing(url: str, config: CommercialAllyConfig) -> OfferLanding:
    try:
        parts = urlsplit(url)
    except ValueError:
        raise _reject("landing_unknown") from None
    host = parts.netloc.lower()
    path = _without_trailing_slash(parts.path)
    # The same on both sides: the manifest accepts an offer url ending in "/",
    # and the event keeps that declared path (the parser and the RPC compare it).
    matches = [
        landing
        for landing in config.offer_landings
        if landing.page_host == host and _without_trailing_slash(landing.page_path) == path
    ]
    if not matches:
        raise _reject("landing_unknown")
    if len(matches) > 1:
        raise _reject("landing_ambiguous")
    return matches[0]


def _phone(raw: str) -> tuple[str, str, str, str]:
    """``(e164, country_code, national, region)`` the parser and the RPC accept.

    ``+521`` followed by exactly ten digits becomes ``+52`` and those ten; no
    other prefix is rewritten (the Argentine ``9`` is not inserted).
    """

    legacy_mx = _MX_LEGACY_MOBILE.fullmatch(raw)
    candidate = f"+52{legacy_mx.group(1)}" if legacy_mx else raw
    try:
        number = phonenumbers.parse(candidate, None)
    except phonenumbers.NumberParseException:
        raise _reject("phone_unusable", phone_region="unknown") from None
    country_region = phonenumbers.region_code_for_country_code(number.country_code)
    region = phonenumbers.region_code_for_number(number)
    if (
        not phonenumbers.is_valid_number(number)
        or region is None
        or _REGION.fullmatch(region) is None
    ):
        raise _reject("phone_unusable", phone_region=country_region)
    e164 = phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)
    if not _RPC_PHONE_DIGITS.fullmatch(e164[1:]):
        # phonenumbers gives valid numbers of 7 digits in all (Niue, +683 4002),
        # the parser accepts them, and the RPC demands identity.phone of 8 to 15
        # digits: 22023 on every delivery, which the route would answer 503.
        raise _reject("phone_unusable", phone_region=region)
    country_code = str(number.country_code)
    national = e164[1 + len(country_code) :]
    if national != str(number.national_number):
        # A national number with a leading zero (Italy): the parser demands
        # phone == +cc+national AND national == national_number, so it would
        # come out phone_valid = false, which the 1.1.0 admission rejects.
        raise _reject("phone_unusable", phone_region=region)
    return e164, country_code, national, region


def _utc_instant(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(UTC)


def translate_ghl_form_submission(
    body: Mapping[str, object],
    *,
    config: CommercialAllyConfig,
    allowed_forms: frozenset[str],
    now: datetime,
) -> GhlTranslation:
    """The ``lead.precheckout`` 1.1.0 event for one GHL form submission.

    Raises :class:`GhlAdapterRejection`. ``id`` is a new random ULID on every
    call (never derived from GHL): a GHL retry is one more submission that the
    admission links to the same live intent, with no conflict row.
    ``created_at`` is ``now`` (GHL sends no submission time). ``allowed_forms``
    is ``[adaptadores.ghl].formularios`` of the manifest.
    """

    instant = _utc_instant(now)
    if not isinstance(body, Mapping):
        raise _reject("invalid_payload")

    # 1. The contact, or 400.
    contact_id = _text(body.get("contact_id"))
    email_raw = _text(body.get("email"))
    phone_raw = _text(body.get("phone"))
    name = _buyer_name(body)
    if contact_id is None or email_raw is None or phone_raw is None or name is None:
        raise _reject("invalid_payload")
    email = email_raw.lower()
    # The email is identity: one with a NUL or a lone surrogate is out of shape,
    # never silently rewritten into another address. One longer than an SMTP
    # address can be (254) is out of shape too: past ~2.7 KB it no longer fits the
    # btree row of purchase_intents_one_observed_email_idx, the RPC fails on every
    # delivery and GHL would retry forever.
    if (
        _EMAIL.fullmatch(email) is None
        or _UNSTORABLE.search(email) is not None
        or len(email) > _EMAIL_MAX_LENGTH
    ):
        raise _reject("invalid_payload")

    # 2. The submission that fired the workflow: only the top-level object. The
    #    GHL editor test sends it empty.
    submission = body.get("attributionSource")
    if not isinstance(submission, Mapping):
        raise _reject("not_a_form_submission")
    url = _text(submission.get("url"))
    form_id = submission.get("mediumId")
    if (
        url is None
        or submission.get("medium") != "form"
        or not isinstance(form_id, str)
        or not form_id
    ):
        raise _reject("not_a_form_submission")

    # 3. The form, which carries the consent claim.
    if form_id not in allowed_forms:
        raise _reject("form_not_allowed")

    # 4. The landing and its offer.
    landing = _offer_landing(url, config)

    # 5. Attribution: only from the URL query of that same object.
    query = urlsplit(url).query
    params = _search_params(query)
    object_fbclid = submission.get("fbclid")
    fbclid = _first(params, "fbclid") or (
        object_fbclid if isinstance(object_fbclid, str) else ""
    )
    referrer = submission.get("referrer")
    attribution = {field: _first(params, field) for field in _UTM_FIELDS}
    attribution["sck"] = compose_lancemos_sck(query)
    attribution["fbclid"] = fbclid
    attribution["referrer"] = referrer if isinstance(referrer, str) else ""
    # Free text from a URL anyone can write: what cannot be stored is dropped and
    # the lead is kept.
    attribution = {field: _storable(value) for field, value in attribution.items()}

    # 6. The phone.
    phone, country_code, national, region = _phone(phone_raw)

    # 7-8. The event.
    price = config.product_price
    event: dict[str, object] = {
        "id": generate_issuance_ulid(
            timestamp_ms=(instant - _EPOCH) // timedelta(milliseconds=1)
        ),
        "event": "lead.precheckout",
        "version": "1.1.0",
        "created_at": instant.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "source": {
            "system": "landing",
            "site": landing.site,
            "aliado": config.lead_ally_name,
            "landing_id": landing.landing_id,
            "page_url": f"https://{landing.page_host}{landing.page_path}",
        },
        "data": {
            "buyer": {
                "name": name,
                "email": email,
                "phone": phone,
                "phone_country_code": country_code,
                "phone_national": national,
            },
            "product": {
                "hotlink": config.product_hotlink,
                "id": None,
                "name": config.product_name,
                "price": int(price) if price == price.to_integral_value() else float(price),
                "currency": config.currency,
            },
            "offer": {"code": landing.offer_code},
            "checkout_url": (
                f"https://pay.hotmart.com/{config.product_hotlink}?off={landing.offer_code}"
            ),
            "checkout_country": {"iso": region, "source": "phone_country_code"},
            "attribution": attribution,
            "consent": {
                "marketing_optin": True,
                "whatsapp_contact": True,
                "copy_version": config.consent_copy_version,
            },
        },
        "dedupe_key": f"{landing.site}:{landing.offer_code}:{email}",
    }
    return GhlTranslation(
        event=event,
        form_id=form_id,
        site=landing.site,
        landing_id=landing.landing_id,
        offer_code=landing.offer_code,
        phone_region=region,
        has_utm=any(_first(params, f"utm_{field}") for field in (*_SCK_FIELDS, "id")),
        has_fbclid=bool(attribution["fbclid"]),
    )
