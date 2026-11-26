"""Formularios propios del recuperador (docs/contracts/own-forms-v1.md).

Reemplazan al formulario de GHL sin cambiar lo que pasa despues: un envio se
traduce al mismo ``lead.precheckout`` 1.1.0 que arma el adaptador de GHL
(``ghl_precheckout_adapter``), con sus mismas reglas de borde (telefono, ``sck``
del core, caracteres que la base no guarda), y la ruta lo admite por la misma
admision portable. Lo que cambia es de donde salen los datos: del formulario que
dibuja el bridge (pagina propia o ``embed.js``) y no del cuerpo de GHL.

Puro: sin red, sin base y sin importar ``app``. La definicion de cada formulario
sale de ``[[formularios_propios]]`` del manifiesto (``instance_manifest.OwnForm``).
"""

from __future__ import annotations

import json
import re
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib import resources
from types import MappingProxyType
from urllib.parse import urlencode, urlsplit

import phonenumbers

from .checkout_issuance import generate_issuance_ulid
from .commercial_ally import CommercialAllyConfig, OfferLanding
from .ghl_precheckout_adapter import (
    GhlAdapterRejection,
    _first,
    _phone,
    _search_params,
    _storable,
    _UNSTORABLE,
    compose_lancemos_sck,
)

OWN_FORMS_PATH_PREFIX = "/f"
OWN_FORMS_CONTRACT = "own-forms-v1"
# El cuerpo de un envio: cinco textos cortos. Lo de mas es un abuso.
OWN_FORM_BODY_LIMIT_BYTES = 8 * 1024
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_EMAIL_MAX_LENGTH = 254
_NAME_MAX_LENGTH = 200
_URL_MAX_LENGTH = 2048
_ASCII_TRIM_CHARS = " \t\n\r\f\v"
_UTM_FIELDS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_CONFIG_MARKER = "/*__SF_CONFIG__*/null"
# El checkout de Hotmart del core (lancemos/core src/superpowers/checkouts.js).
_HOTMART_CHECKOUT_MODE = "10"
_HOTMART_UTMS = ("source", "medium", "campaign", "content", "term")

# Los paises del selector, en este orden, cuando el formulario no declara los
# suyos: el publico de las aliadas de Lancemos (Mexico primero).
DEFAULT_COUNTRIES = (
    "MX", "US", "CO", "AR", "PE", "CL", "EC", "VE", "GT", "ES", "DO", "CR",
    "PA", "UY", "BO", "PY", "HN", "SV", "NI", "PR", "CA",
)
COUNTRY_NAMES: Mapping[str, str] = MappingProxyType(
    {
        "MX": "México", "US": "Estados Unidos", "CO": "Colombia", "AR": "Argentina",
        "PE": "Perú", "CL": "Chile", "EC": "Ecuador", "VE": "Venezuela",
        "GT": "Guatemala", "ES": "España", "DO": "República Dominicana",
        "CR": "Costa Rica", "PA": "Panamá", "UY": "Uruguay", "BO": "Bolivia",
        "PY": "Paraguay", "HN": "Honduras", "SV": "El Salvador", "NI": "Nicaragua",
        "PR": "Puerto Rico", "CA": "Canadá", "BR": "Brasil", "CU": "Cuba",
        "IT": "Italia", "FR": "Francia", "DE": "Alemania", "GB": "Reino Unido",
    }
)

# Lo que ve la persona cuando el envio no sirve. El codigo va al log.
USER_MESSAGES: Mapping[str, str] = MappingProxyType(
    {
        "nombre": "Escribe tu nombre completo.",
        "email": "Revisa tu correo.",
        "telefono": "Revisa tu número de WhatsApp: no parece un número válido.",
    }
)


class OwnFormRejection(ValueError):
    """El envio no puede ser un ``lead.precheckout``. ``str()`` es solo el codigo."""

    def __init__(self, code: str, *, phone_region: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.phone_region = phone_region

    @property
    def user_message(self) -> str:
        return USER_MESSAGES.get(self.code, "Revisa tus datos e intenta de nuevo.")


@dataclass(frozen=True)
class OwnFormSubmission:
    """Lo que manda el navegador, ya leido como texto (sin validar)."""

    name: str
    email: str
    phone: str
    page_url: str
    referrer: str
    honeypot: str
    fbc: str = ""

    @classmethod
    def from_json(cls, body: object) -> OwnFormSubmission:
        if not isinstance(body, Mapping):
            raise OwnFormRejection("cuerpo_invalido")

        def text(key: str) -> str:
            value = body.get(key, "")
            return value if isinstance(value, str) else ""

        return cls(
            name=text("nombre"),
            email=text("email"),
            phone=text("telefono"),
            page_url=text("pagina")[:_URL_MAX_LENGTH],
            referrer=text("referrer")[:_URL_MAX_LENGTH],
            honeypot=text("website"),
            fbc=text("fbc")[:512],
        )


@dataclass(frozen=True)
class OwnFormTranslation:
    event: dict[str, object]  # lead.precheckout 1.1.0, listo para parse_lead_precheckout
    checkout_url: str
    phone_region: str
    has_utm: bool
    has_fbclid: bool


def _clean(value: str) -> str | None:
    cleaned = _storable(value).strip(_ASCII_TRIM_CHARS)
    return cleaned or None


def _fbclid_from_fbc(fbc: str) -> str:
    # La cookie _fbc es fb.<subdominio>.<creacion>.<fbclid>: el cuarto campo.
    parts = fbc.split(".", 3)
    return parts[3] if len(parts) == 4 and parts[0] == "fb" else ""


def hotmart_checkout_url(
    config: CommercialAllyConfig,
    landing: OfferLanding,
    *,
    name: str,
    email: str,
    phone: str,
    country_code: str,
    national: str,
    attribution: Mapping[str, str],
) -> str:
    """El link de Hotmart que arma el core al enviar el formulario (checkouts.js).

    ``off``, ``checkoutMode=10``, el contacto prellenado (con el telefono partido
    en ``phoneac`` + ``phonenumber``), las UTM, ``sck`` y ``fbclid``. No lleva
    ``src`` (la clave de VTurb es de la pagina del core, que arma su propio link
    cuando el formulario esta embebido en ella) ni el ``_gl`` de GA4.
    """

    params: list[tuple[str, str]] = [
        ("off", landing.offer_code),
        ("checkoutMode", _HOTMART_CHECKOUT_MODE),
        ("name", name),
        ("email", email),
        ("phone", phone),
        ("phoneac", country_code),
        ("phonenumber", national),
    ]
    for key in _HOTMART_UTMS:
        value = attribution.get(f"utm_{key}", "")
        if value:
            params.append((f"utm_{key}", value))
    if attribution.get("sck"):
        params.append(("sck", attribution["sck"]))
    if attribution.get("fbclid"):
        params.append(("fbclid", attribution["fbclid"]))
    return f"https://pay.hotmart.com/{config.product_hotlink}?{urlencode(params)}"


def translate_own_form_submission(
    submission: OwnFormSubmission,
    *,
    config: CommercialAllyConfig,
    landing: OfferLanding,
    now: datetime,
) -> OwnFormTranslation:
    """El ``lead.precheckout`` 1.1.0 de un envio de un formulario propio.

    El mismo evento que ``translate_ghl_form_submission``: misma forma, mismo
    ``id`` nuevo por envio, mismo ``dedupe_key``, mismo ``page_url`` (el de la
    landing declarada) y el consentimiento de la instancia. La landing sale de la
    definicion del formulario (D2); la atribucion, de la URL de la pagina donde
    esta el formulario.
    """

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    instant = now.astimezone(UTC)

    name = _clean(submission.name)
    if name is None or len(name.replace(" ", "")) < 2 or len(name) > _NAME_MAX_LENGTH:
        raise OwnFormRejection("nombre")
    email_raw = _clean(submission.email)
    email = email_raw.lower() if email_raw is not None else ""
    if (
        _EMAIL.fullmatch(email) is None
        or _UNSTORABLE.search(email) is not None
        or len(email) > _EMAIL_MAX_LENGTH
    ):
        raise OwnFormRejection("email")
    phone_raw = _clean(submission.phone)
    if phone_raw is None or not phone_raw.startswith("+"):
        raise OwnFormRejection("telefono", phone_region="unknown")
    try:
        phone, country_code, national, region = _phone(phone_raw)
    except GhlAdapterRejection as exc:
        raise OwnFormRejection("telefono", phone_region=exc.phone_region) from None

    try:
        query = urlsplit(submission.page_url).query
    except ValueError:
        query = ""
    params = _search_params(query)
    fbclid = _first(params, "fbclid") or _fbclid_from_fbc(submission.fbc)
    attribution = {field: _first(params, field) for field in _UTM_FIELDS}
    attribution["sck"] = compose_lancemos_sck(query)
    attribution["fbclid"] = fbclid
    attribution["referrer"] = submission.referrer
    attribution = {field: _storable(value) for field, value in attribution.items()}

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
    checkout_url = hotmart_checkout_url(
        config,
        landing,
        name=name,
        email=email,
        phone=phone,
        country_code=country_code,
        national=national,
        attribution=attribution,
    )
    return OwnFormTranslation(
        event=event,
        checkout_url=checkout_url,
        phone_region=region,
        has_utm=any(_first(params, f"utm_{field}") for field in (*_HOTMART_UTMS, "id")),
        has_fbclid=bool(fbclid),
    )


def webhook_copy(event: Mapping[str, object], *, form_key: str) -> dict[str, object]:
    """Lo que se le manda a un webhook declarado: el contacto y de donde vino.

    Paridad con la accion *Webhook* de GHL (D9). Sin el ``id`` interno ni el
    ``dedupe_key``.
    """

    data = event["data"]
    source = event["source"]
    assert isinstance(data, Mapping) and isinstance(source, Mapping)
    return {
        "event": "own_form.submitted",
        "version": "1",
        "form": form_key,
        "created_at": event["created_at"],
        "contact": data["buyer"],
        "landing": {
            "site": source["site"],
            "landing_id": source["landing_id"],
            "page_url": source["page_url"],
        },
        "offer": data["offer"],
        "attribution": data["attribution"],
        "consent": data["consent"],
    }


# ------------------------------------------------------------------ limits


class SlidingWindowLimiter:
    """Un limite en memoria por clave: ``limit`` eventos cada ``window_s`` segundos.

    Por proceso (el bridge corre un worker de uvicorn). Es la barrera barata
    contra un bot (D4); Turnstile queda para despues.
    """

    def __init__(self, limit: int, window_s: float, *, max_keys: int = 50_000) -> None:
        self.limit = limit
        self.window_s = window_s
        self.max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        if len(self._hits) > self.max_keys:
            self._hits.clear()
        hits = self._hits.setdefault(key, deque())
        while hits and current - hits[0] >= self.window_s:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(current)
        return True


# ------------------------------------------------------------------ assets


def _asset(name: str) -> str:
    return resources.files("bridge").joinpath("own_forms_assets", name).read_text(
        encoding="utf-8"
    )


def country_options(countries: tuple[str, ...]) -> list[dict[str, str]]:
    options = []
    for iso in countries:
        code = phonenumbers.country_code_for_region(iso)
        if code:
            options.append(
                {"iso": iso, "nombre": COUNTRY_NAMES.get(iso, iso), "prefijo": str(code)}
            )
    return options


def render_embed_js(form: object, *, submit_url: str) -> str:
    """``embed.js`` con la configuracion del formulario inyectada."""

    config = {
        "clave": form.key,  # type: ignore[attr-defined]
        "enviar_url": submit_url,
        "titulo": form.title,  # type: ignore[attr-defined]
        "boton": form.button,  # type: ignore[attr-defined]
        "aclaracion": form.disclaimer,  # type: ignore[attr-defined]
        "links": [
            {"texto": text, "url": url}
            for text, url in form.links  # type: ignore[attr-defined]
        ],
        "pais_por_defecto": form.default_country,  # type: ignore[attr-defined]
        "paises": country_options(form.countries),  # type: ignore[attr-defined]
        "etiquetas": {
            "nombre": "Nombre completo",
            "email": "Correo electrónico",
            "telefono": "WhatsApp",
        },
        "css": _asset("form.css"),
        "usar_core": form.use_core,  # type: ignore[attr-defined]
    }
    # Seguro dentro de un <script>: sin "</" ni separadores de linea de JS.
    payload = (
        json.dumps(config, ensure_ascii=False)
        .replace("</", "<\\/")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )
    source = _asset("embed.js")
    if source.count(_CONFIG_MARKER) != 1:
        raise RuntimeError("embed.js must contain the config marker exactly once")
    return source.replace(_CONFIG_MARKER, payload)


def _html_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def render_page_html(form: object, *, embed_url: str) -> str:
    """La pagina propia: el formulario solo, con su propio ``embed.js``."""

    return (
        _asset("page.html")
        .replace("{{TITULO_PAGINA}}", _html_escape(form.page_title))  # type: ignore[attr-defined]
        .replace("{{CLAVE}}", _html_escape(form.key))  # type: ignore[attr-defined]
        .replace("{{EMBED_URL}}", _html_escape(embed_url))
    )
