"""La solicitud de plantilla de Meta: el borrador, sus reglas, el pedido y la burbuja (template-request-v1).

Contrato: docs/contracts/template-request-v1.md. Diseno: F-146 (E-258), §3 y §6.

Cuando un flujo necesita una plantilla que no existe, el Automation Expert la
redacta con el duenio. Este modulo es puro (no lee el entorno, no hace red ni
toca bases) y hace cuatro cosas:

* ``parse_template``: lee el borrador que escribe el agente, con los datos
  entre llaves con nombre (``Hola {{nombre}}``) y los botones por lo que
  significan (``checkout``, ``ficha``, ``url_fija``, ``respuesta``), y lo
  normaliza: el orden en que quedan numeradas las variables sale de aca.
* ``check``: las reglas. Las de Meta son las de ``meta_plantillas.validar`` del
  OS (``.claude/scripts/integrations/meta_plantillas.py``), portadas, y las del
  producto se suman: solo variables del vocabulario del flujo, un solo boton de
  URL y primero, la URL dinamica solo a un destino que el bridge sabe llenar,
  la URL fija solo de la lista de la instancia (D7), el nombre con el prefijo
  de la instancia y su numero, el idioma de la instancia, y ni links ni
  telefonos ni mails en el texto.
* ``meta_request``: el cuerpo exacto de ``POST /{WABA}/message_templates``
  (como ``meta_plantillas.pedido``): posicional, con los ejemplos que exige
  Meta. Los ejemplos los pone el bridge, nunca el agente: un nombre generico
  fijo y el producto real de la instancia.
* ``bubble``: el mensaje como lo veria la persona, para la pagina y para que el
  agente pregunte «¿asi?».

Nada de esto llama a Meta: el envio es de la parte 3 de F-146.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .approved_templates import CHECKOUT_BUTTON_URL
from .flow_definition import VARIABLES

__all__ = [
    "CATEGORIES",
    "CONTRACT",
    "EXAMPLE_NAME",
    "OPT_OUT_BUTTON",
    "RULES_VERSION",
    "Draft",
    "Finding",
    "TemplateContext",
    "TemplateRequestInvalid",
    "TemplateResult",
    "bubble",
    "check",
    "check_for_step",
    "context_from_manifest",
    "draft_from_stored",
    "meta_request",
    "number_body",
    "parse_template",
    "step_at",
]

CONTRACT = "template-request-v1"
# Sube cuando cambia una regla: la validacion guardada dice con cuales corrio.
RULES_VERSION = 1
CATEGORIES = ("UTILITY", "MARKETING")
# La baja se detecta por este texto (opt_out.py normaliza los acentos).
OPT_OUT_BUTTON = "No más mensajes"
# El nombre del ejemplo para Meta: generico y fijo, nunca el de una persona.
EXAMPLE_NAME = "María"
BODY_MAX_CHARS = 1024
HEADER_MAX_CHARS = 60
FOOTER_MAX_CHARS = 60
BUTTON_TEXT_MAX_CHARS = 25
BUTTONS_MAX = 10
BUTTON_KINDS = ("checkout", "ficha", "url_fija", "respuesta")
URL_KINDS = ("checkout", "ficha", "url_fija")
# El ejemplo de la ficha: inventado, el mismo que se le paso a Meta para las de OXXO (E-195).
SLIP_EXAMPLE_SUFFIX = "0123456789abcdef0123456789abcdef0123456789abcdef"
# Los ejemplos de los datos que no salen del manifiesto (de las plantillas aprobadas de ATT1).
EXAMPLE_WHEN = "hoy a las 3:10 p. m."
EXAMPLE_DUE = "mañana"
EXAMPLE_PAYMENT_METHOD = "OXXO"

_NAMED = re.compile(r"\{\{\s*([^{}\s]*)\s*\}\}")
_POSITIONAL = re.compile(r"\{\{\s*\d+\s*\}\}")
_PROMO = re.compile(r"(?i)descuento|\boferta\b|promoci|cup[oó]n|\bgratis\b")
_LINK = re.compile(r"(?i)https?://|\bwww\.")
# Un dominio suelto (pay.hotmart.com/X, marca.com.mx): WhatsApp tambien lo vuelve link.
_BARE_DOMAIN = re.compile(
    r"(?i)\b[a-z0-9-]+(\.[a-z0-9-]+)*\.(com|net|org|mx|ar|co|cl|pe|es|us|io|app|link|ly|me|info|site|online|"
    r"shop|store|page|gle|tv|biz|xyz)\b")
_FORMATTING = re.compile(r"[*_~`]")
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_PHONE_SEPARATORS = frozenset(" \t\n\r -.()+")
_LANGUAGE = re.compile(r"[a-z]{2,3}(_[A-Z]{2})?")
_NAME = re.compile(r"[a-z0-9][a-z0-9_]{1,99}")
_FIXED_URL = re.compile(r"https://[^\s{}#\"'<>]+")
_TEMPLATE_KEYS = {"nombre", "idioma", "categoria", "cuerpo", "encabezado", "pie", "botones"}
_TEMPLATE_REQUIRED = {"nombre", "idioma", "categoria", "cuerpo"}


class TemplateRequestInvalid(Exception):
    """El borrador no tiene la forma: un codigo cerrado y que campo no cierra (nunca un valor)."""

    def __init__(self, code: str, reason: Mapping[str, Any]) -> None:
        super().__init__(code)
        self.code = code
        self.reason = dict(reason)


@dataclass(frozen=True)
class TemplateContext:
    """Lo que el validador sabe de la instancia, del manifiesto.

    ``fixed_urls`` es la lista de URLs que un boton fijo puede llevar
    (``[plantillas].urls_fijas``, parte 6 de F-146): vacia, ningun boton fijo
    pasa. ``slip_buttons`` son las bases del boton de la ficha
    (``[[ficha.medios]]``): ``(medio, tienda, url con {{1}})``.
    """

    ally_ref: str
    language: str | None
    product_name: str
    hotlink: str
    offer_code: str
    price: Decimal | None = None
    currency: str | None = None
    fixed_urls: tuple[str, ...] = ()
    slip_buttons: tuple[tuple[str, str, str], ...] = ()


@dataclass(frozen=True)
class Draft:
    """El borrador normalizado: lo que guarda la base (``meta_template_requests.draft``)."""

    name: str
    language: str
    category: str
    body: str
    variables: tuple[str, ...]
    buttons: tuple[Mapping[str, str], ...]
    header: str | None = None
    footer: str | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"nombre": self.name, "idioma": self.language, "categoria": self.category,
                               "cuerpo": self.body, "variables": list(self.variables),
                               "botones": [dict(button) for button in self.buttons]}
        if self.header is not None:
            out["encabezado"] = self.header
        if self.footer is not None:
            out["pie"] = self.footer
        return out


@dataclass(frozen=True)
class Finding:
    code: str
    path: str
    reason: str

    def to_json(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "reason": self.reason}


@dataclass
class TemplateResult:
    blocks: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blocks

    def block(self, code: str, path: str, reason: str) -> None:
        self.blocks.append(Finding(code, path, reason))

    def warn(self, code: str, path: str, reason: str) -> None:
        self.warnings.append(Finding(code, path, reason))

    def to_json(self) -> dict[str, list[dict[str, str]]]:
        return {"blocks": [f.to_json() for f in self.blocks], "warnings": [f.to_json() for f in self.warnings]}


def context_from_manifest(manifest: Any, *, fixed_urls: Sequence[str] = ()) -> TemplateContext:
    """El contexto del validador, del manifiesto de la instancia (``InstanceManifest``).

    El idioma es el de sus plantillas, si es uno solo; la oferta del ejemplo, la
    oferta por defecto. Las URLs fijas todavia no estan en el manifiesto (parte 6):
    hasta entonces llegan vacias y ningun boton fijo pasa.
    """
    languages = {template.language for template in manifest.templates.values()}
    offer = next((o for o in manifest.offers if o.default), manifest.offers[0] if manifest.offers else None)
    return TemplateContext(
        ally_ref=manifest.ally_ref,
        language=next(iter(languages)) if len(languages) == 1 else None,
        product_name=manifest.product_name,
        hotlink=manifest.hotlink,
        offer_code=offer.code if offer is not None else "",
        price=manifest.price,
        currency=manifest.currency,
        fixed_urls=tuple(fixed_urls),
        slip_buttons=tuple((method.medio, method.tienda, method.button_url_template)
                           for method in manifest.payment_slip_methods if method.habilitada),
    )


# ---------------------------------------------------------------------------
# La forma.
# ---------------------------------------------------------------------------


def _invalid(field_name: str, problem: str) -> TemplateRequestInvalid:
    return TemplateRequestInvalid("template_request_invalid", {"field": field_name, "problem": problem})


def _text(raw: Mapping[str, Any], key: str, *, required: bool) -> str | None:
    if key not in raw:
        if required:
            raise _invalid(f"template.{key}", "missing")
        return None
    value = raw[key]
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"template.{key}", "not_a_text")
    if "\x00" in value:
        raise _invalid(f"template.{key}", "not_a_text")
    return value


def number_body(body: str) -> tuple[str, tuple[str, ...]]:
    """Cambia ``{{nombre}}`` por ``{{1}}``... en orden de aparicion (``meta_plantillas.numerar``)."""
    order: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in order:
            order.append(name)
        return "{{%d}}" % (order.index(name) + 1)

    return _NAMED.sub(replace, body), tuple(order)


def parse_template(raw: Any) -> Draft:
    """El borrador del agente, normalizado. La forma; las reglas son de ``check``."""
    if not isinstance(raw, Mapping):
        raise _invalid("template", "not_object")
    keys = set(raw)
    missing, extra = _TEMPLATE_REQUIRED - keys, keys - _TEMPLATE_KEYS
    if missing or extra:
        reason: dict[str, Any] = {"field": "template"}
        if missing:
            reason["missing"] = sorted(missing)
        if extra:
            reason["extra"] = sorted(key if isinstance(key, str) and re.fullmatch(r"[a-z_]{1,40}", key)
                                     else "<otra>" for key in extra)[:10]
        raise TemplateRequestInvalid("template_request_invalid", reason)
    name = _text(raw, "nombre", required=True)
    language = _text(raw, "idioma", required=True)
    category = raw.get("categoria")
    if category not in CATEGORIES:
        raise _invalid("template.categoria", "utility_or_marketing")
    body = _text(raw, "cuerpo", required=True)
    assert name is not None and language is not None and body is not None
    if len(name) > 100 or _NAME.fullmatch(name) is None:
        raise _invalid("template.nombre", "lowercase_digits_and_underscore")
    if _LANGUAGE.fullmatch(language) is None:
        raise _invalid("template.idioma", "meta_language_code")
    if len(body) > 4096:
        raise _invalid("template.cuerpo", "too_long")
    header = _text(raw, "encabezado", required=False)
    footer = _text(raw, "pie", required=False)
    for key, value in (("encabezado", header), ("pie", footer)):
        if value is not None and len(value) > 200:
            raise _invalid(f"template.{key}", "too_long")
    buttons_raw = raw.get("botones", [])
    if not isinstance(buttons_raw, list) or len(buttons_raw) > 20:
        raise _invalid("template.botones", "list_of_up_to_20")
    buttons: list[dict[str, str]] = []
    for index, button in enumerate(buttons_raw):
        at = f"template.botones.{index}"
        if not isinstance(button, Mapping) or button.get("tipo") not in BUTTON_KINDS:
            raise _invalid(at, "tipo_checkout_ficha_url_fija_or_respuesta")
        allowed = {"tipo", "texto"} | ({"url"} if button["tipo"] == "url_fija" else set()) | (
            {"medio"} if button["tipo"] == "ficha" else set())
        if set(button) - allowed or "texto" not in button:
            raise _invalid(at, "keys")
        text = button["texto"]
        if not isinstance(text, str) or not text.strip() or len(text) > 100:
            raise _invalid(f"{at}.texto", "not_a_text")
        out = {"tipo": button["tipo"], "texto": text}
        if button["tipo"] == "url_fija":
            url = button.get("url")
            if not isinstance(url, str) or not url or len(url) > 2000:
                raise _invalid(f"{at}.url", "not_a_text")
            out["url"] = url
        if "medio" in button:
            medio = button["medio"]
            if not isinstance(medio, str) or re.fullmatch(r"[A-Z][A-Z_]{1,39}", medio) is None:
                raise _invalid(f"{at}.medio", "hotmart_payment_type")
            out["medio"] = medio
        buttons.append(out)
    _, variables = number_body(body)
    return Draft(name=name, language=language, category=category, body=body, variables=variables,
                 buttons=tuple(buttons), header=header, footer=footer)


def draft_from_stored(stored: Any) -> Draft:
    """El borrador que guardo la base, leido de nuevo con las mismas reglas de forma."""
    if not isinstance(stored, Mapping):
        raise _invalid("draft", "not_object")
    raw = {key: value for key, value in stored.items() if key != "variables"}
    draft = parse_template(raw)
    if list(draft.variables) != stored.get("variables"):
        raise _invalid("draft.variables", "order_mismatch")
    return draft


# ---------------------------------------------------------------------------
# Las reglas.
# ---------------------------------------------------------------------------


def _normalized(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return " ".join("".join(c for c in decomposed if unicodedata.category(c) != "Mn").split())


def _looks_like_identifier(text: str) -> bool:
    """Un mail, o siete digitos seguidos sin contar separadores (la regla de la lectura del AE)."""
    if _EMAIL.search(text):
        return True
    run = 0
    for character in text:
        if character in _PHONE_SEPARATORS:
            continue
        run = run + 1 if character.isnumeric() else 0
        if run >= 7:
            return True
    return False


def _slip_button(draft_button: Mapping[str, str], context: TemplateContext) -> tuple[str, str, str] | None:
    """La base del boton de la ficha que le toca: la unica, o la del ``medio`` que nombra."""
    medio = draft_button.get("medio")
    options = [option for option in context.slip_buttons if medio is None or option[0] == medio]
    distinct = {option[2] for option in options}
    if len(distinct) != 1:
        return None
    return options[0]


def check(draft: Draft, context: TemplateContext, *, catalog_names: Sequence[tuple[str, str]] | None = None
          ) -> TemplateResult:
    """Las reglas de Meta (``meta_plantillas.validar``) y las del producto.

    ``catalog_names`` son los pares (nombre, idioma) del catalogo de la bandeja,
    en cualquier estado; con ``None`` (no se pudo leer) no se mira.
    """
    result = TemplateResult()
    body, header, footer = draft.body, draft.header, draft.footer

    # El nombre: el de Meta, con el prefijo de la instancia y su numero.
    prefix = context.ally_ref.replace("-", "_") + "_"
    if re.fullmatch(re.escape(prefix) + r"[a-z0-9]+(_[a-z0-9]+)*_[0-9]{2}", draft.name) is None:
        result.block("template_name_pattern", "/nombre",
                     f"El nombre va como {prefix}<proposito>_01: el prefijo de la instancia y un numero de dos "
                     "cifras, que sube al corregir un rechazo.")
    if catalog_names is not None and any(name == draft.name for name, _ in catalog_names):
        result.block("template_name_in_catalog", "/nombre",
                     "Ya hay una plantilla con ese nombre en la cuenta: se usa el numero siguiente.")
    if context.language is None:
        result.block("template_language_unknown", "/idioma",
                     "La instancia no tiene un idioma unico en sus plantillas: no se puede elegir.")
    elif draft.language != context.language:
        result.block("template_language_not_instance", "/idioma",
                     f"Las plantillas de la instancia van en {context.language}.")

    # El cuerpo.
    if len(body) > BODY_MAX_CHARS:
        result.block("template_body_too_long", "/cuerpo", f"El cuerpo tiene hasta {BODY_MAX_CHARS} caracteres.")
    if _POSITIONAL.search(body):
        result.block("template_variable_numbered", "/cuerpo",
                     "Los datos van con nombre, entre llaves ({{nombre}}): el bridge los numera.")
    used = _NAMED.findall(body)
    for name in sorted(set(used)):
        if name.isdigit():
            continue
        if name not in VARIABLES:
            result.block("template_variable_unknown", "/cuerpo",
                         "Solo datos que el flujo sabe llenar: " + ", ".join(VARIABLES) + ".")
        if used.count(name) > 1:
            result.block("template_variable_repeated", "/cuerpo",
                         "Cada dato va una sola vez: el producto lo manda una vez.")
    stripped = body.strip()
    if _NAMED.match(stripped) or re.search(r"\}\}\s*$", stripped):
        result.block("template_variable_at_edge", "/cuerpo",
                     "Meta rechaza un cuerpo que empieza o termina con un dato.")
    if re.search(r"\}\}\s*\{\{", body):
        result.block("template_variables_adjacent", "/cuerpo", "Meta rechaza dos datos seguidos.")
    if used and len(_NAMED.sub("", body).split()) < 3 * len(used):
        result.warn("template_few_words_for_variables", "/cuerpo",
                    "Pocas palabras para tantos datos: Meta la puede rechazar.")
    if "\n\n\n" in body:
        result.block("template_blank_lines", "/cuerpo", "Mas de una linea en blanco seguida.")
    if "{" in _NAMED.sub("", body) or "}" in _NAMED.sub("", body) or any(
            not name.isdigit() and (not name or not re.fullmatch(r"[a-z_]+", name)) for name in used):
        result.block("template_variable_malformed", "/cuerpo",
                     "Un dato va entre dos llaves, con su nombre sin espacios: {{nombre}}.")
    if "precio" in used and context.price is None:
        result.block("template_price_unknown", "/cuerpo",
                     "El manifiesto no tiene precio: el ejemplo para Meta no se puede armar.")
    texts_to_scan = [("cuerpo", body), ("encabezado", header), ("pie", footer)] + [
        (f"botones/{index}/texto", button["texto"]) for index, button in enumerate(draft.buttons)]
    for key, value in texts_to_scan:
        if value is None:
            continue
        if _LINK.search(value) or _BARE_DOMAIN.search(value):
            result.block("template_text_has_link", f"/{key}",
                         "Un link va solo en un boton, y de la lista de la instancia.")
        if _looks_like_identifier(value):
            result.block("identifier_not_allowed", f"/{key}", "Ni telefonos ni mails en el texto de una plantilla.")

    # El encabezado y el pie: texto fijo.
    for key, value, limit in (("encabezado", header, HEADER_MAX_CHARS), ("pie", footer, FOOTER_MAX_CHARS)):
        if value is None:
            continue
        if len(value) > limit:
            result.block(f"template_{'header' if key == 'encabezado' else 'footer'}_too_long", f"/{key}",
                         f"El {key} de texto va de 1 a {limit} caracteres.")
        if "{" in value or "}" in value:
            result.block(f"template_{'header' if key == 'encabezado' else 'footer'}_has_variable", f"/{key}",
                         f"El {key} va fijo, sin datos.")
        if "\n" in value or "\r" in value:
            result.block(f"template_{'header' if key == 'encabezado' else 'footer'}_has_line_break", f"/{key}",
                         f"El {key} va en una sola linea.")
    if header is not None and (_FORMATTING.search(header) or any(ord(c) > 0xFFFF for c in header)):
        # A verificar en la documentacion de Meta: el encabezado de texto no lleva formato ni emojis.
        result.warn("template_header_formatting", "/encabezado",
                    "Meta puede rechazar formato (*, _, ~) o emojis en el encabezado.")

    # Los botones.
    buttons = draft.buttons
    if len(buttons) > BUTTONS_MAX:
        result.block("template_too_many_buttons", "/botones", f"Hasta {BUTTONS_MAX} botones.")
    texts = [button["texto"] for button in buttons]
    for index, button in enumerate(buttons):
        at = f"/botones/{index}"
        if len(button["texto"]) > BUTTON_TEXT_MAX_CHARS:
            result.block("template_button_text_too_long", f"{at}/texto",
                         f"El texto de un boton va hasta {BUTTON_TEXT_MAX_CHARS} caracteres.")
        if texts.count(button["texto"]) > 1:
            result.block("template_button_text_repeated", f"{at}/texto", "Dos botones con el mismo texto.")
        if any(c in button["texto"] for c in "{}\n\r"):
            result.block("template_button_text_invalid", f"{at}/texto",
                         "El texto de un boton va en una linea y sin datos: el bridge no los llena.")
        if button["tipo"] == "url_fija":
            url = button.get("url", "")
            if _FIXED_URL.fullmatch(url) is None:
                result.block("template_fixed_url_invalid", f"{at}/url",
                             "La URL fija va con https:// y sin datos.")
            elif url not in context.fixed_urls:
                result.block("template_fixed_url_not_listed", f"{at}/url",
                             "Una URL fija solo de la lista de la instancia ([plantillas].urls_fijas).")
        if button["tipo"] == "ficha" and _slip_button(button, context) is None:
            result.block("template_slip_button_unavailable", f"{at}",
                         "El link de la ficha solo con un medio de [[ficha.medios]] de la instancia.")
    url_buttons = [index for index, button in enumerate(buttons) if button["tipo"] in URL_KINDS]
    if len(url_buttons) > 1:
        result.block("template_more_than_one_url_button", "/botones",
                     "Un solo boton de link, como acepta hoy el producto.")
    if url_buttons and url_buttons[0] != 0:
        result.block("template_url_button_not_first", f"/botones/{url_buttons[0]}",
                     "El boton de link va primero y las respuestas rapidas despues.")
    if url_buttons and len(buttons) > 1:
        result.warn("template_url_and_quick_replies", "/botones",
                    "Un link con respuestas rapidas no se ve en WhatsApp de escritorio.")
    replies = [_normalized(button["texto"]) for button in buttons if button["tipo"] == "respuesta"]
    if draft.category == "MARKETING" and _normalized(OPT_OUT_BUTTON) not in replies:
        result.block("template_marketing_without_opt_out", "/botones",
                     f"Una plantilla de marketing lleva la respuesta «{OPT_OUT_BUTTON}»: la baja se detecta por "
                     "ese texto.")
    if draft.category == "UTILITY" and _PROMO.search(" ".join(filter(None, (header, body, footer)))):
        result.warn("template_utility_sounds_promotional", "/categoria",
                    "Es de utilidad y habla de descuento u oferta: Meta la puede aprobar como marketing.")
    return result


# ---------------------------------------------------------------------------
# El pedido de Meta y la burbuja.
# ---------------------------------------------------------------------------


def _examples(context: TemplateContext) -> dict[str, str]:
    price = None
    if context.price is not None:
        amount = format(context.price.normalize(), "f")
        price = f"${amount} {context.currency}" if context.currency else f"${amount}"
    store = context.slip_buttons[0][1] if context.slip_buttons else EXAMPLE_PAYMENT_METHOD
    return {"nombre": EXAMPLE_NAME, "producto": context.product_name, "medio_de_pago": store,
            "cuando": EXAMPLE_WHEN, "precio": price or "", "vence": EXAMPLE_DUE}


def meta_request(draft: Draft, context: TemplateContext) -> dict[str, Any]:
    """El cuerpo de ``POST /{WABA}/message_templates`` (``meta_plantillas.pedido``), posicional.

    Lo arma solo un borrador sin bloqueos: un boton de ficha sin base o una
    variable fuera del vocabulario son ``ValueError``.
    """
    numbered, order = number_body(draft.body)
    examples = _examples(context)
    body: dict[str, Any] = {"type": "BODY", "text": numbered}
    if order:
        if any(name not in examples for name in order):
            raise ValueError("template_variable_unknown")
        body["example"] = {"body_text": [[examples[name] for name in order]]}
    components: list[dict[str, Any]] = []
    if draft.header is not None:
        components.append({"type": "HEADER", "format": "TEXT", "text": draft.header})
    components.append(body)
    if draft.footer is not None:
        components.append({"type": "FOOTER", "text": draft.footer})
    buttons: list[dict[str, Any]] = []
    for button in draft.buttons:
        if button["tipo"] == "checkout":
            buttons.append({"type": "URL", "text": button["texto"], "url": CHECKOUT_BUTTON_URL,
                            "example": [f"https://pay.hotmart.com/{context.hotlink}?off={context.offer_code}"
                                        "&sck=ejemplo"]})
        elif button["tipo"] == "ficha":
            slip = _slip_button(button, context)
            if slip is None:
                raise ValueError("template_slip_button_unavailable")
            buttons.append({"type": "URL", "text": button["texto"], "url": slip[2],
                            "example": [slip[2].replace("{{1}}", SLIP_EXAMPLE_SUFFIX)]})
        elif button["tipo"] == "url_fija":
            buttons.append({"type": "URL", "text": button["texto"], "url": button["url"]})
        else:
            buttons.append({"type": "QUICK_REPLY", "text": button["texto"]})
    if buttons:
        components.append({"type": "BUTTONS", "buttons": buttons})
    return {"name": draft.name, "language": draft.language, "category": draft.category,
            "parameter_format": "POSITIONAL", "components": components}


_LINK_WORDS = {"checkout": "link de pago", "ficha": "link de la ficha"}
CATEGORY_MEANING = {
    "UTILITY": ("Utilidad: sobre algo que la persona ya hizo (su compra, su pago, su turno). Si Meta entiende que "
                "vende, la aprueba como marketing y un flujo que la pide de utilidad no la puede usar."),
    "MARKETING": ("Marketing: invita a comprar. Lleva el boton «No más mensajes», cuesta mas por mensaje y Meta puede "
                  "no entregarla a quien ya recibio muchas de marketing."),
}


def bubble(draft: Draft, context: TemplateContext) -> dict[str, Any]:
    """El mensaje como lo veria la persona: con los datos entre corchetes, y con un ejemplo."""
    examples = _examples(context)

    def with_marks(text: str | None) -> str | None:
        return None if text is None else _NAMED.sub(lambda match: f"[{match.group(1)}]", text)

    def with_example(text: str | None) -> str | None:
        return None if text is None else _NAMED.sub(
            lambda match: examples.get(match.group(1), f"[{match.group(1)}]"), text)

    buttons = []
    for button in draft.buttons:
        shown: dict[str, str] = {"tipo": button["tipo"], "texto": button["texto"]}
        if button["tipo"] in _LINK_WORDS:
            shown["link"] = _LINK_WORDS[button["tipo"]]
        elif button["tipo"] == "url_fija":
            shown["link"] = button["url"]
        buttons.append(shown)
    return {
        "encabezado": draft.header,
        "cuerpo": with_marks(draft.body),
        "cuerpo_con_ejemplo": with_example(draft.body),
        "pie": draft.footer,
        "botones": buttons,
        "categoria": draft.category,
        "categoria_explicada": CATEGORY_MEANING[draft.category],
        "al_aprobar": ("Se manda a Meta. Nadie recibe nada todavia. Cuando Meta conteste se avisa, y el flujo se "
                       "aprueba aparte."),
    }


def check_for_step(draft: Draft, definition: Any, step: Any, result: TemplateResult) -> None:
    """Lo que el paso del flujo que la espera le pide (las reglas 5, 13 y 17 del validador de flujos).

    El paso tiene que esperarla con ``{a_crear: <nombre>}``; si nombra idioma o
    variables, tienen que ser los del borrador; el boton del paso (``checkout`` o
    ``ficha``) tiene que ser el primero de la plantilla, y sin boton en el paso
    la plantilla no lleva un link que cambia por persona; ``precio`` y ``vence``
    van solo con cupon, y una plantilla de marketing no va en un flujo con
    permiso de compra.
    """
    named = step.get("plantilla") if isinstance(step, Mapping) else None
    if not isinstance(named, Mapping) or named.get("a_crear") != draft.name:
        result.block("template_flow_step_mismatch", "/for_flow/step",
                     "El paso de la propuesta no espera esta plantilla ({a_crear: <nombre>}).")
        return
    language = step.get("idioma")
    if isinstance(language, str) and language != draft.language:
        result.block("template_flow_step_mismatch", "/idioma", "El paso la pide en otro idioma.")
    variables = step.get("variables")
    if isinstance(variables, list) and variables and list(variables) != list(draft.variables):
        result.block("template_flow_step_mismatch", "/cuerpo",
                     "El paso manda otros datos, o en otro orden: " + ", ".join(map(str, variables)) + ".")
    button = step.get("boton") if isinstance(step.get("boton"), Mapping) else None
    link = button.get("link") if button is not None else None
    kinds = [b["tipo"] for b in draft.buttons]
    if link in ("checkout", "ficha") and (not kinds or kinds[0] != link):
        result.block("template_flow_step_mismatch", "/botones", f"El paso manda el link {link}: va primero.")
    if link is None and any(kind in ("checkout", "ficha") for kind in kinds):
        result.block("template_flow_step_mismatch", "/botones",
                     "El paso no manda un link que cambia por persona: la plantilla no lo puede llevar.")
    if (button is None or "cupon" not in button) and any(v in draft.variables for v in ("precio", "vence")):
        result.block("template_flow_step_mismatch", "/cuerpo", "precio y vence van solo con un cupon en el paso.")
    entra = definition.get("entra") if isinstance(definition, Mapping) else None
    if isinstance(entra, Mapping) and entra.get("permiso") == "compra" and draft.category == "MARKETING":
        result.block("template_flow_step_mismatch", "/categoria",
                     "Un flujo con permiso de compra manda solo plantillas de utilidad.")


def step_at(items: Any, pointer: str) -> Any:
    """El paso de una propuesta de flujo en ``pointer`` (JSON Pointer sobre ``{"items": [...]}``), o None."""
    node: Any = {"items": items}
    if not pointer.startswith("/"):
        return None
    for part in pointer[1:].split("/"):
        if isinstance(node, Mapping) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return None
    return node
