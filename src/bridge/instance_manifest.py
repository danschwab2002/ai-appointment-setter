"""Manifiesto de instancia v2: todo lo no secreto que describe una instancia del setter.

Diseno: docs/design/setter-producto-instalable-v1.md §5 (ADR-0021). Referencia de
campos: docs/referencia-manifiesto.md.

Una instancia es una aliada: un numero de WhatsApp con su voz y sus productos. El
manifiesto vive en el repo privado de la instancia (``instancia.toml``) y el
producto lo lee; el codigo no sabe quien es el cliente.

El formato es TOML porque lo lee la biblioteca estandar (``tomllib``): el producto
no suma una dependencia para leer su configuracion, y TOML no convierte en
booleano un codigo de oferta como ``no`` u ``off``.

Mientras los caminos del bridge sigan leyendo ``CommercialAllyConfig`` (v1), el
manifiesto v2 se traduce a ese binding con ``to_commercial_ally_config``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
import tomllib
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .commercial_ally import CommercialAllyConfig, OfferLanding

SCHEMA = "setter-instancia/v2"

_REF = re.compile(r"[a-z0-9][a-z0-9-]{0,127}")
_VERSION = re.compile(r"v\d+\.\d+\.\d+")
_CURRENCY = re.compile(r"[A-Z]{3}")
_OFFER_CODE = re.compile(r"[A-Za-z0-9]{4,32}")
_HOTLINK = re.compile(r"[A-Za-z0-9]{4,32}")
_TEMPLATE_NAME = re.compile(r"[a-z0-9_]{1,512}")
_LANGUAGE = re.compile(r"[a-z]{2}(?:_[A-Z]{2})?")
_SLACK_CHANNEL = re.compile(r"[CG][A-Z0-9]{6,20}")

EVENTS = ("intencion", "carrito", "pago_fallido", "compra", "entrante")
FLOWS = ("inbound", "precheckout", "carrito", "pago_fallido", "reactivacion", "descuento")
TEMPLATE_SLOTS = ("precheckout", "carrito", "pago_fallido", "reactivacion", "descuento")
# Las variables del cuerpo que una plantilla de primer contacto puede pedir, en el
# orden en que la plantilla aprobada las numera ({{1}}, {{2}}). Por defecto son las
# dos que el bridge manda hoy. Reactivacion y descuento arman sus parametros en su
# propio modulo, asi que no aceptan ``parametros``.
TEMPLATE_PARAMETERS = ("nombre", "producto")
DEFAULT_TEMPLATE_PARAMETERS = ("nombre", "producto")
PARAMETERIZED_TEMPLATE_SLOTS = ("precheckout", "carrito", "pago_fallido")
OFFER_ORIGINS = ("pauta", "organico", "otro")

# Lo que cada flujo necesita para poder prenderse: el evento que lo dispara y la
# plantilla de Meta con la que abre la conversacion. Un flujo cuyo evento no existe
# en la instancia queda apagado por construccion (§6.4 del diseno).
FLOW_REQUIREMENTS: Mapping[str, tuple[str, str | None]] = MappingProxyType({
    "inbound": ("entrante", None),
    "precheckout": ("intencion", "precheckout"),
    "carrito": ("carrito", "carrito"),
    "pago_fallido": ("pago_fallido", "pago_fallido"),
    "reactivacion": ("entrante", "reactivacion"),
    "descuento": ("entrante", "descuento"),
})

_TOP_KEYS_REQUIRED = {
    "schema",
    "producto",
    "instancia",
    "hotmart",
    "chatwoot",
    "inbound",
    "consentimiento",
    "plantillas",
    "agente",
    "eventos",
    "flujos",
    "guardas",
}
_TOP_KEYS_OPTIONAL = {"slack", "revision_diaria"}


class ManifestError(ValueError):
    """El manifiesto no es valido. El mensaje dice que campo y por que."""


@dataclass(frozen=True)
class Offer:
    code: str
    site: str
    landing_id: str
    url: str
    origin: str
    default: bool

    @property
    def page_host(self) -> str:
        return urlsplit(self.url).hostname or ""

    @property
    def page_path(self) -> str:
        return urlsplit(self.url).path


@dataclass(frozen=True)
class Template:
    """Una plantilla aprobada en Meta.

    ``parameters`` son las variables del cuerpo en orden ({{1}}, {{2}}). Solo lo leen
    las plantillas de primer contacto (``PARAMETERIZED_TEMPLATE_SLOTS``).
    """

    name: str
    language: str
    parameters: tuple[str, ...] = DEFAULT_TEMPLATE_PARAMETERS


@dataclass(frozen=True)
class InstanceManifest:
    product_version: str
    tenant_ref: str
    ally_ref: str
    funnel_ref: str
    binding_version: int
    brand: str
    time_zone: str
    hotmart_product_id: int
    hotlink: str
    product_name: str
    currency: str
    price: Decimal
    offers: tuple[Offer, ...]
    chatwoot_account_id: int
    chatwoot_inbox_id: int
    handoff_team_id: int | None
    inbound_scope_key: str
    inbound_scope_version: int
    consent_copy_version: str
    templates: Mapping[str, Template]
    agent_model_name: str
    agent_knowledge_path: str
    events: frozenset[str]
    flows: Mapping[str, bool]
    sensitive_subjects: tuple[str, ...]
    sensitive_actions: tuple[str, ...]
    slack_channel: str | None
    daily_review_reviewers: tuple[str, ...]

    @property
    def default_offer(self) -> Offer:
        return next(offer for offer in self.offers if offer.default)

    def offer_for_landing(self, site: str, landing_id: str) -> Offer | None:
        for offer in self.offers:
            if offer.site == site and offer.landing_id == landing_id:
                return offer
        return None

    def checkout_url(self, offer: Offer | None = None) -> str:
        chosen = offer or self.default_offer
        return f"https://pay.hotmart.com/{self.hotlink}?off={chosen.code}"

    def flow_blockers(self, flow: str) -> tuple[str, ...]:
        """Lo que le falta a la instancia para poder prender ``flow``."""

        event, slot = FLOW_REQUIREMENTS[flow]
        missing = []
        if event not in self.events:
            missing.append(f"el evento '{event}' no esta en eventos")
        if slot is not None and slot not in self.templates:
            missing.append(f"falta la plantilla '{slot}'")
        return tuple(missing)

    def to_commercial_ally_config(self) -> CommercialAllyConfig:
        """El binding v1 que leen hoy los caminos del bridge.

        La oferta por defecto es la del binding; las demas landings van en
        ``additional_offer_codes`` (F2c), asi un carrito o un pago fallido que
        entra por cualquier landing de la instancia se admite, y su sitio, host
        y ruta en ``additional_offer_landings`` (A6), asi tambien se admite el
        formulario del precheckout de cada landing.
        """

        offer = self.default_offer
        others = tuple(other for other in self.offers if other is not offer)
        return CommercialAllyConfig(
            tenant_ref=self.tenant_ref,
            funnel_ref=self.funnel_ref,
            binding_version=self.binding_version,
            ally_ref=self.ally_ref,
            lead_ally_name=self.brand,
            lead_site=offer.site,
            lead_landing_id=offer.landing_id,
            lead_page_host=offer.page_host,
            lead_page_path=offer.page_path,
            product_hotlink=self.hotlink,
            product_name=self.product_name,
            product_price=self.price,
            currency=self.currency,
            offer_code=offer.code,
            additional_offer_codes=tuple(other.code for other in others),
            additional_offer_landings=tuple(
                OfferLanding(
                    offer_code=other.code,
                    site=other.site,
                    landing_id=other.landing_id,
                    page_host=other.page_host,
                    page_path=other.page_path,
                )
                for other in others
            ),
            consent_copy_version=self.consent_copy_version,
            hotmart_product_id=self.hotmart_product_id,
            chatwoot_account_id=self.chatwoot_account_id,
            chatwoot_inbox_id=self.chatwoot_inbox_id,
            inbound_scope_key=self.inbound_scope_key,
            inbound_scope_version=self.inbound_scope_version,
        )

    @classmethod
    def from_toml_file(cls, path: Path) -> InstanceManifest:
        try:
            payload = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            raise ManifestError(f"no se pudo leer {path}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise ManifestError(f"{path} no es TOML valido: {exc}") from exc
        return cls.from_mapping(payload)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> InstanceManifest:
        _exact_keys("manifiesto", payload, _TOP_KEYS_REQUIRED, _TOP_KEYS_OPTIONAL)
        if payload["schema"] != SCHEMA:
            raise ManifestError(f"schema debe ser '{SCHEMA}'")
        product_version = _str(payload, "producto", "producto")
        if _VERSION.fullmatch(product_version) is None:
            raise ManifestError("producto debe ser una version vX.Y.Z")

        instancia = _table(payload, "instancia")
        _exact_keys(
            "instancia",
            instancia,
            {"tenant_ref", "ally_ref", "funnel_ref", "binding_version", "marca", "zona_horaria"},
        )
        tenant_ref = _ref(instancia, "tenant_ref", "instancia.tenant_ref")
        ally_ref = _ref(instancia, "ally_ref", "instancia.ally_ref")
        funnel_ref = _ref(instancia, "funnel_ref", "instancia.funnel_ref")
        binding_version = _positive_int(instancia, "binding_version", "instancia.binding_version")
        brand = _str(instancia, "marca", "instancia.marca")
        time_zone = _str(instancia, "zona_horaria", "instancia.zona_horaria")
        try:
            ZoneInfo(time_zone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ManifestError(f"instancia.zona_horaria '{time_zone}' no es una zona IANA") from exc

        hotmart = _table(payload, "hotmart")
        _exact_keys(
            "hotmart",
            hotmart,
            {"product_id", "hotlink", "product_name", "moneda", "precio", "ofertas"},
        )
        hotmart_product_id = _positive_int(hotmart, "product_id", "hotmart.product_id")
        hotlink = _str(hotmart, "hotlink", "hotmart.hotlink")
        if _HOTLINK.fullmatch(hotlink) is None:
            raise ManifestError("hotmart.hotlink debe ser alfanumerico")
        product_name = _str(hotmart, "product_name", "hotmart.product_name")
        currency = _str(hotmart, "moneda", "hotmart.moneda")
        if _CURRENCY.fullmatch(currency) is None:
            raise ManifestError("hotmart.moneda debe ser un codigo de tres letras mayusculas")
        price = _price(hotmart.get("precio"))
        offers = _offers(hotmart.get("ofertas"))

        chatwoot = _table(payload, "chatwoot")
        _exact_keys("chatwoot", chatwoot, {"account_id", "inbox_id"}, {"equipo_derivacion"})
        handoff_team_id = (
            _positive_int(chatwoot, "equipo_derivacion", "chatwoot.equipo_derivacion")
            if "equipo_derivacion" in chatwoot
            else None
        )

        inbound = _table(payload, "inbound")
        _exact_keys("inbound", inbound, {"scope_key", "scope_version"})

        consentimiento = _table(payload, "consentimiento")
        _exact_keys("consentimiento", consentimiento, {"copy_version"})

        templates = _templates(_table(payload, "plantillas"))

        agente = _table(payload, "agente")
        _exact_keys("agente", agente, {"modelo", "conocimiento"})
        knowledge_path = _str(agente, "conocimiento", "agente.conocimiento")
        if knowledge_path.startswith("/") or ".." in Path(knowledge_path).parts:
            raise ManifestError(
                "agente.conocimiento debe ser una ruta relativa dentro de la instancia"
            )

        events = _events(payload.get("eventos"))
        flows = _flows(_table(payload, "flujos"))

        guardas = _table(payload, "guardas")
        _exact_keys("guardas", guardas, {"terminos_sensibles", "acciones_sensibles"})

        slack_channel = None
        if "slack" in payload:
            slack = _table(payload, "slack")
            _exact_keys("slack", slack, set(), {"canal"})
            if "canal" in slack:
                slack_channel = _str(slack, "canal", "slack.canal")
                if _SLACK_CHANNEL.fullmatch(slack_channel) is None:
                    raise ManifestError("slack.canal debe ser un ID de canal de Slack (C...)")
        reviewers: tuple[str, ...] = ()
        if "revision_diaria" in payload:
            review = _table(payload, "revision_diaria")
            _exact_keys("revision_diaria", review, set(), {"revisores"})
            reviewers = _str_list(review.get("revisores", []), "revision_diaria.revisores")

        manifest = cls(
            product_version=product_version,
            tenant_ref=tenant_ref,
            ally_ref=ally_ref,
            funnel_ref=funnel_ref,
            binding_version=binding_version,
            brand=brand,
            time_zone=time_zone,
            hotmart_product_id=hotmart_product_id,
            hotlink=hotlink,
            product_name=product_name,
            currency=currency,
            price=price,
            offers=offers,
            chatwoot_account_id=_positive_int(chatwoot, "account_id", "chatwoot.account_id"),
            chatwoot_inbox_id=_positive_int(chatwoot, "inbox_id", "chatwoot.inbox_id"),
            handoff_team_id=handoff_team_id,
            inbound_scope_key=_ref(inbound, "scope_key", "inbound.scope_key"),
            inbound_scope_version=_positive_int(inbound, "scope_version", "inbound.scope_version"),
            consent_copy_version=_str(consentimiento, "copy_version", "consentimiento.copy_version"),
            templates=templates,
            agent_model_name=_str(agente, "modelo", "agente.modelo"),
            agent_knowledge_path=knowledge_path,
            events=events,
            flows=flows,
            sensitive_subjects=_str_list(guardas["terminos_sensibles"], "guardas.terminos_sensibles"),
            sensitive_actions=_str_list(guardas["acciones_sensibles"], "guardas.acciones_sensibles"),
            slack_channel=slack_channel,
            daily_review_reviewers=reviewers,
        )
        for flow, enabled in manifest.flows.items():
            blockers = manifest.flow_blockers(flow)
            if enabled and blockers:
                raise ManifestError(
                    f"flujos.{flow} esta prendido pero " + "; ".join(blockers)
                )
        # El binding v1 valida hosts, rutas y slugs con sus propias reglas: si el v2
        # no se puede traducir, el manifiesto no sirve para los caminos de hoy.
        try:
            manifest.to_commercial_ally_config()
        except ValueError as exc:
            raise ManifestError(f"el manifiesto no produce un binding valido: {exc}") from exc
        return manifest


def _exact_keys(
    where: str,
    table: Mapping[str, Any],
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    optional = optional or set()
    missing = sorted(required - set(table))
    extra = sorted(set(table) - required - optional)
    if missing:
        raise ManifestError(f"{where}: faltan {', '.join(missing)}")
    if extra:
        raise ManifestError(f"{where}: claves no soportadas {', '.join(extra)}")


def _table(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ManifestError(f"{key} debe ser una tabla")
    return value


def _str(table: Mapping[str, Any], key: str, where: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ManifestError(f"{where} debe ser un texto no vacio, sin espacios en los bordes")
    return value


def _ref(table: Mapping[str, Any], key: str, where: str) -> str:
    value = _str(table, key, where)
    if _REF.fullmatch(value) is None:
        raise ManifestError(f"{where} debe ser un slug en minusculas (a-z, 0-9, guiones)")
    return value


def _positive_int(table: Mapping[str, Any], key: str, where: str) -> int:
    value = table.get(key)
    if type(value) is not int or value < 1:
        raise ManifestError(f"{where} debe ser un entero positivo")
    return value


def _price(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ManifestError("hotmart.precio debe ser un texto decimal (\"47\") o un entero")
    try:
        price = Decimal(str(value))
    except InvalidOperation as exc:
        raise ManifestError("hotmart.precio no es un decimal") from exc
    if not price.is_finite() or price <= 0:
        raise ManifestError("hotmart.precio debe ser positivo")
    return price


def _str_list(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() == item and item for item in value
    ):
        raise ManifestError(f"{where} debe ser una lista de textos no vacios")
    if len(set(value)) != len(value):
        raise ManifestError(f"{where} tiene elementos repetidos")
    return tuple(value)


def _offers(value: object) -> tuple[Offer, ...]:
    if not isinstance(value, list) or not value:
        raise ManifestError("hotmart.ofertas debe tener al menos una oferta")
    offers = []
    for index, raw in enumerate(value):
        where = f"hotmart.ofertas[{index}]"
        if not isinstance(raw, dict):
            raise ManifestError(f"{where} debe ser una tabla")
        _exact_keys(where, raw, {"codigo", "site", "landing_id", "url", "origen"}, {"por_defecto"})
        code = _str(raw, "codigo", f"{where}.codigo")
        if _OFFER_CODE.fullmatch(code) is None:
            raise ManifestError(f"{where}.codigo debe ser alfanumerico")
        url = _str(raw, "url", f"{where}.url")
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.port
            or parts.query
            or parts.fragment
            or not parts.path.startswith("/")
        ):
            raise ManifestError(f"{where}.url debe ser https://host/ruta, sin query ni fragmento")
        origin = _str(raw, "origen", f"{where}.origen")
        if origin not in OFFER_ORIGINS:
            raise ManifestError(f"{where}.origen debe ser uno de {', '.join(OFFER_ORIGINS)}")
        default = raw.get("por_defecto", False)
        if type(default) is not bool:
            raise ManifestError(f"{where}.por_defecto debe ser true o false")
        offers.append(
            Offer(
                code=code,
                site=_ref(raw, "site", f"{where}.site"),
                landing_id=_ref(raw, "landing_id", f"{where}.landing_id"),
                url=url,
                origin=origin,
                default=default,
            )
        )
    if sum(offer.default for offer in offers) != 1:
        raise ManifestError("hotmart.ofertas debe tener exactamente una oferta con por_defecto = true")
    codes = [offer.code for offer in offers]
    if len(set(codes)) != len(codes):
        raise ManifestError("hotmart.ofertas tiene codigos repetidos")
    landings = [(offer.site, offer.landing_id) for offer in offers]
    if len(set(landings)) != len(landings):
        raise ManifestError("hotmart.ofertas tiene dos ofertas para la misma landing")
    return tuple(offers)


def _templates(table: Mapping[str, Any]) -> Mapping[str, Template]:
    _exact_keys("plantillas", table, set(), set(TEMPLATE_SLOTS))
    templates = {}
    for slot, raw in table.items():
        where = f"plantillas.{slot}"
        if not isinstance(raw, dict):
            raise ManifestError(f"{where} debe ser una tabla {{ nombre, idioma }}")
        if "parametros" in raw and slot not in PARAMETERIZED_TEMPLATE_SLOTS:
            raise ManifestError(
                f"{where}.parametros no se admite: solo lo aceptan "
                + ", ".join(PARAMETERIZED_TEMPLATE_SLOTS)
            )
        _exact_keys(
            where,
            raw,
            {"nombre", "idioma"},
            {"parametros"} if slot in PARAMETERIZED_TEMPLATE_SLOTS else set(),
        )
        name = _str(raw, "nombre", f"{where}.nombre")
        if _TEMPLATE_NAME.fullmatch(name) is None:
            raise ManifestError(f"{where}.nombre debe ser el nombre de Meta (a-z, 0-9, _)")
        language = _str(raw, "idioma", f"{where}.idioma")
        if _LANGUAGE.fullmatch(language) is None:
            raise ManifestError(f"{where}.idioma debe ser un codigo de Meta como es_MX")
        parameters = (
            _template_parameters(raw["parametros"], f"{where}.parametros")
            if "parametros" in raw
            else DEFAULT_TEMPLATE_PARAMETERS
        )
        templates[slot] = Template(name=name, language=language, parameters=parameters)
    return MappingProxyType(templates)


def _template_parameters(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= len(TEMPLATE_PARAMETERS):
        raise ManifestError(
            f"{where} debe ser una lista de 1 a {len(TEMPLATE_PARAMETERS)} variables"
        )
    unknown = [item for item in value if item not in TEMPLATE_PARAMETERS]
    if unknown:
        raise ManifestError(
            f"{where}: variables no soportadas {', '.join(map(str, unknown))} "
            f"(validas: {', '.join(TEMPLATE_PARAMETERS)})"
        )
    if len(set(value)) != len(value):
        raise ManifestError(f"{where} tiene variables repetidas")
    return tuple(value)


def _events(value: object) -> frozenset[str]:
    events = _str_list(value, "eventos")
    unknown = sorted(set(events) - set(EVENTS))
    if unknown:
        raise ManifestError(
            f"eventos no soportados: {', '.join(unknown)} (validos: {', '.join(EVENTS)})"
        )
    return frozenset(events)


def _flows(table: Mapping[str, Any]) -> Mapping[str, bool]:
    _exact_keys("flujos", table, set(FLOWS))
    for flow, enabled in table.items():
        if type(enabled) is not bool:
            raise ManifestError(f"flujos.{flow} debe ser true o false")
    return MappingProxyType({flow: table[flow] for flow in FLOWS})
