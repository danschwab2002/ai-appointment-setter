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
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import re
import tomllib
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .commercial_ally import CommercialAllyConfig, OfferLanding
from .payment_slip import (
    PAYMENT_SLIP_TEMPLATE_PARAMETERS,
    PaymentSlipMethod,
    parse_payment_slip_methods,
)

SCHEMA = "setter-instancia/v2"

_REF = re.compile(r"[a-z0-9][a-z0-9-]{0,127}")
_VERSION = re.compile(r"v\d+\.\d+\.\d+")
_CURRENCY = re.compile(r"[A-Z]{3}")
_OFFER_CODE = re.compile(r"[A-Za-z0-9]{4,32}")
_HOTLINK = re.compile(r"[A-Za-z0-9]{4,32}")
_TEMPLATE_NAME = re.compile(r"[a-z0-9_]{1,512}")
_LANGUAGE = re.compile(r"[a-z]{2}(?:_[A-Z]{2})?")
_SLACK_CHANNEL = re.compile(r"[CG][A-Z0-9]{6,20}")
# El id de un formulario de GHL, tal como llega en attributionSource.mediumId. Los
# dos medidos (2026-09-29) tienen 20 alfanumericos; un id fuera de esa forma falla al
# cargar, en voz alta, y no en cada envio.
_GHL_FORM_ID = re.compile(r"[A-Za-z0-9]{20}")
# El contrato cuyo riesgo acepta por escrito quien firma ``[adaptadores.ghl]``
# (docs/contracts/ghl-precheckout-adapter-v1.md, Riesgos). Cuando esa seccion
# cambie de fondo el producto sube esta constante y una aceptacion escrita
# contra el contrato anterior deja de cargar, a proposito: una aceptacion
# vencida tiene que verse, no degradar en silencio.
GHL_ADAPTER_RISK_CONTRACT = "ghl-precheckout-adapter-v1"
_GHL_RISK_KEYS = ("riesgo_aceptado_por", "riesgo_aceptado_el", "riesgo_contrato")
# En que landing vive cada formulario (``landing_por_formulario``) y que hace el
# adaptador con eso (``landing_por_formulario_modo``). Van las dos o ninguna.
_GHL_FORM_LANDING_KEYS = ("landing_por_formulario", "landing_por_formulario_modo")
# sombra: la landing sigue saliendo de la URL y el log anota cuando el formulario
# la habria corregido. activo: la landing sale del formulario.
GHL_FORM_LANDING_MODES = ("sombra", "activo")
# Los ejemplos de la documentacion firman con un marcador entre < y >: copiado
# sin editar no es una firma, y no carga.
_GHL_RISK_PLACEHOLDER_CHARS = ("<", ">")
# Los flujos que usan una intencion como permiso o como audiencia sin mirar la
# base: con [adaptadores.ghl] y sin la aceptacion no se prenden.
GHL_RISK_GATED_FLOWS = ("precheckout", "pago_fallido")
# La ficha de pago (C1) tambien usa la intencion como permiso: su planificador
# se deriva del del pago fallido. No entra en GHL_RISK_GATED_FLOWS para no
# cambiar lo que esa lista ya publica (el log del arranque y validate); la
# guarda del riesgo la suma aparte (INTENT_PERMISSION_FLOWS).
INTENT_PERMISSION_FLOWS = GHL_RISK_GATED_FLOWS + ("ficha",)

EVENTS = ("intencion", "carrito", "pago_fallido", "compra", "entrante", "ficha_impresa")
FLOWS = ("inbound", "precheckout", "carrito", "pago_fallido", "reactivacion", "descuento")
# Los flujos que un manifiesto puede no declarar: sin la clave, el flujo esta
# apagado y ``flows`` no la trae, asi que un manifiesto anterior carga y se lee
# igual que antes. ``flow_enabled`` los lee a los dos.
OPTIONAL_FLOWS = ("ficha",)
TEMPLATE_SLOTS = (
    "precheckout", "carrito", "pago_fallido", "reactivacion", "descuento", "ficha",
)
# Las variables del cuerpo que una plantilla de primer contacto puede pedir, en el
# orden en que la plantilla aprobada las numera ({{1}}, {{2}}). Por defecto son las
# dos que el bridge manda hoy. Reactivacion y descuento arman sus parametros en su
# propio modulo, asi que no aceptan ``parametros``.
TEMPLATE_PARAMETERS = ("nombre", "producto")
DEFAULT_TEMPLATE_PARAMETERS = ("nombre", "producto")
PARAMETERIZED_TEMPLATE_SLOTS = ("precheckout", "carrito", "pago_fallido", "ficha")
# La plantilla de la ficha tiene su propio vocabulario: el nombre y el producto,
# como las otras, y la tienda y el vencimiento que salen de [[ficha.medios]]
# (docs/contracts/portable-payment-slip-first-contact-v1.md). Por defecto, las
# cuatro en el orden de la plantilla aprobada de ATT1.
SLOT_TEMPLATE_PARAMETERS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "ficha": PAYMENT_SLIP_TEMPLATE_PARAMETERS,
})
SLOT_DEFAULT_TEMPLATE_PARAMETERS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "ficha": PAYMENT_SLIP_TEMPLATE_PARAMETERS,
})
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
    "ficha": ("ficha_impresa", "ficha"),
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
_TOP_KEYS_OPTIONAL = {"slack", "revision_diaria", "adaptadores", "productos", "ficha"}

# La etiqueta de Chatwoot de un producto. Chatwoot pasa el titulo a minusculas y
# acepta letras, numeros, guiones y guiones bajos.
_CHATWOOT_LABEL = re.compile(r"[a-z0-9][a-z0-9_-]{0,254}")
# Las etiquetas de la automatizacion. Un producto no puede llamarse igual: su
# macro pondria la pausa o la baja en cada conversacion del producto.
RESERVED_CHATWOOT_LABELS = ("automation_paused", "automation_opted_out")
# Los productos de una aliada. El primero es el principal: lo suyo sale de
# [hotmart] y del funnel de [instancia]. Desde el segundo, cada uno trae su
# funnel y su Hotmart (paso 2c de varios productos por aliada).
MAX_PRODUCTS = 8
# Lo que el principal no puede traer en [[productos]]: ya esta arriba.
_PRINCIPAL_ONLY_ABOVE = ("funnel_ref", "binding_version", "hotmart")


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
class GhlRiskAcceptance:
    """La aceptacion escrita del riesgo del adaptador de GHL.

    El token del adaptador es la unica barrera de sus envios y lo lee cualquier
    usuario de la subcuenta de GHL; una intencion que entro por el adaptador no
    se distingue en la base de la de una landing. Quien firma acepta que esas
    intenciones se usen como permiso de contacto y como audiencia. No caduca ni
    se ata a la lista de formularios: sumar un formulario ya es un cambio
    firmado del manifiesto.
    """

    accepted_by: str
    accepted_on: date
    contract: str


@dataclass(frozen=True)
class GhlFormLandings:
    """``landing_por_formulario`` de ``[adaptadores.ghl]``.

    GHL manda en ``attributionSource.url`` la pagina donde empezo la visita, que
    no siempre es la del formulario: quien entro por la landing D y lleno el
    formulario de la B llega con la URL de la D (medido en ATT1 el 2026-10-08).
    El formulario si dice donde se lleno. ``landing_by_form`` va del id del
    formulario al ``landing_id`` de una oferta; un formulario que vive en mas de
    una pagina no se declara y sigue por la URL.
    """

    mode: str
    landing_by_form: Mapping[str, str]


@dataclass(frozen=True)
class ProductCommerce:
    """El funnel y el Hotmart de un producto que no es el principal (paso 2c).

    El principal no lo lleva: lo suyo sale de ``[hotmart]`` y de ``[instancia]``,
    como antes de varios productos. Cada producto es su propio binding en la base,
    con su ``funnel_ref`` (decision de Dan del 2026-10-16).
    """

    funnel_ref: str
    binding_version: int
    hotmart_product_id: int
    hotlink: str
    product_name: str
    currency: str
    price: Decimal
    offers: tuple[Offer, ...]


@dataclass(frozen=True)
class Product:
    """Un producto de la aliada (``[[productos]]``).

    ``key`` es la misma ``clave`` que la seccion del producto en el conocimiento:
    con productos en el manifiesto, el bridge no arranca si el conocimiento no
    separa los mismos (``knowledge_products_error``).

    ``chatwoot_label`` es la etiqueta que el bridge le suma a cada conversacion del
    producto, ejecutando el macro ``chatwoot_label_macro_id``. Va por un macro y no
    por el endpoint de etiquetas de la conversacion, porque ese endpoint reemplaza
    la lista entera y podria borrar ``automation_paused``; la accion ``add_label``
    de un macro solo suma. Las dos van juntas, o ninguna: sin etiqueta, el
    producto no se marca en Chatwoot.

    ``commerce`` es el funnel y el Hotmart de un producto que no es el principal;
    ``None`` en el principal, el primero de la lista.
    """

    key: str
    chatwoot_label: str | None = None
    chatwoot_label_macro_id: int | None = None
    commerce: ProductCommerce | None = None


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
    # Los formularios de GHL que el adaptador traduce a lead.precheckout
    # (docs/contracts/ghl-precheckout-adapter-v1.md). Vacio = sin adaptador.
    ghl_form_ids: tuple[str, ...] = ()
    # La aceptacion escrita del riesgo del adaptador. None = sin aceptacion: con
    # [adaptadores.ghl], precheckout, pago_fallido y una audiencia con
    # consentimiento no arrancan.
    ghl_risk_acceptance: GhlRiskAcceptance | None = None
    # En que landing vive cada formulario. None = la landing sale de la URL.
    ghl_form_landings: GhlFormLandings | None = None
    # Los productos de la aliada ([[productos]]). Vacio = la instancia no declara
    # productos: el bridge no etiqueta nada y el conocimiento puede tener una capa
    # o varias.
    products: tuple[Product, ...] = ()
    # La tabla [[ficha.medios]]: que medio de pago en que pais tiene ficha que
    # se le puede mandar, con su tienda, su vencimiento y la base del boton.
    # Vacia = ningun medio habilitado.
    payment_slip_methods: tuple[PaymentSlipMethod, ...] = ()

    def flow_enabled(self, flow: str) -> bool:
        """Si ``flow`` esta prendido; un flujo opcional sin declarar, no."""

        if flow in OPTIONAL_FLOWS:
            return bool(self.flows.get(flow, False))
        return bool(self.flows[flow])

    @property
    def labeled_product(self) -> Product | None:
        """El producto cuya etiqueta pone el bridge, o None si ninguno tiene."""

        return next(
            (product for product in self.products if product.chatwoot_label is not None),
            None,
        )

    def knowledge_products_error(self, knowledge_keys: tuple[str | None, ...]) -> str | None:
        """Por que el conocimiento no separa los productos del manifiesto, o None.

        ``knowledge_keys`` son las claves de las secciones de producto del
        conocimiento (``None`` en uno de una sola capa). Sin ``[[productos]]`` no
        hay nada que atar.
        """

        if not self.products:
            return None
        declared = sorted(product.key for product in self.products)
        if any(key is None for key in knowledge_keys):
            return (
                "el manifiesto declara [[productos]] y el conocimiento no tiene "
                "secciones por producto"
            )
        found = sorted(key for key in knowledge_keys if key is not None)
        if found != declared:
            return (
                f"los productos del manifiesto ({', '.join(declared)}) no son los "
                f"del conocimiento ({', '.join(found)})"
            )
        return None

    @property
    def ghl_adapter_risk(self) -> str | None:
        """El estado del riesgo del adaptador de GHL que ve el manifiesto.

        * ``accepted`` / ``not_accepted``: hay ``[adaptadores.ghl]``, con o sin
          la aceptacion escrita.
        * ``no_adapter_section``: no hay seccion, pero la instancia recibe
          intenciones y tiene prendido un flujo que las usa como permiso. Si
          alguna vez uso el adaptador, sus intenciones siguen en la base y no
          se distinguen de las de una landing: nada lo bloquea, se avisa.
        * ``None``: no aplica.
        """

        if self.ghl_form_ids:
            return "accepted" if self.ghl_risk_acceptance is not None else "not_accepted"
        if "intencion" in self.events and any(
            self.flow_enabled(flow) for flow in INTENT_PERMISSION_FLOWS
        ):
            return "no_adapter_section"
        return None

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
        if flow == "ficha" and not any(
            method.habilitada for method in self.payment_slip_methods
        ):
            missing.append("no hay ninguna fila habilitada en [[ficha.medios]]")
        return tuple(missing)

    def to_commercial_ally_config(self) -> CommercialAllyConfig:
        """El binding v1 que leen hoy los caminos del bridge: el del principal.

        La oferta por defecto es la del binding; las demas landings van en
        ``additional_offer_codes`` (F2c), asi un carrito o un pago fallido que
        entra por cualquier landing de la instancia se admite, y su sitio, host
        y ruta en ``additional_offer_landings`` (A6), asi tambien se admite el
        formulario del precheckout de cada landing.
        """

        return self._binding(
            funnel_ref=self.funnel_ref,
            binding_version=self.binding_version,
            hotmart_product_id=self.hotmart_product_id,
            hotlink=self.hotlink,
            product_name=self.product_name,
            currency=self.currency,
            price=self.price,
            offers=self.offers,
        )

    def to_commercial_ally_configs(self) -> tuple[CommercialAllyConfig, ...]:
        """Un binding por producto: el del principal primero, despues los demas
        en el orden de ``[[productos]]`` (paso 2c).

        Lo que es de la instancia (la aliada, Chatwoot, el scope entrante y el
        consentimiento) es el mismo en todos; cambia lo del producto.
        """

        return (self.to_commercial_ally_config(),) + tuple(
            self._binding(
                funnel_ref=product.commerce.funnel_ref,
                binding_version=product.commerce.binding_version,
                hotmart_product_id=product.commerce.hotmart_product_id,
                hotlink=product.commerce.hotlink,
                product_name=product.commerce.product_name,
                currency=product.commerce.currency,
                price=product.commerce.price,
                offers=product.commerce.offers,
            )
            for product in self.products
            if product.commerce is not None
        )

    def _binding(
        self,
        *,
        funnel_ref: str,
        binding_version: int,
        hotmart_product_id: int,
        hotlink: str,
        product_name: str,
        currency: str,
        price: Decimal,
        offers: tuple[Offer, ...],
    ) -> CommercialAllyConfig:
        offer = next(candidate for candidate in offers if candidate.default)
        others = tuple(other for other in offers if other is not offer)
        return CommercialAllyConfig(
            tenant_ref=self.tenant_ref,
            funnel_ref=funnel_ref,
            binding_version=binding_version,
            ally_ref=self.ally_ref,
            lead_ally_name=self.brand,
            lead_site=offer.site,
            lead_landing_id=offer.landing_id,
            lead_page_host=offer.page_host,
            lead_page_path=offer.page_path,
            product_hotlink=hotlink,
            product_name=product_name,
            product_price=price,
            currency=currency,
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
            hotmart_product_id=hotmart_product_id,
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

        (
            hotmart_product_id,
            hotlink,
            product_name,
            currency,
            price,
            offers,
        ) = _hotmart(_table(payload, "hotmart"), "hotmart")

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
        products = _products(payload.get("productos"))
        payment_slip_methods = _payment_slip_methods(payload)
        # landing_por_formulario puede apuntar a la landing de cualquier producto.
        all_offers = offers + tuple(
            offer
            for product in products
            if product.commerce is not None
            for offer in product.commerce.offers
        )
        ghl_form_ids, ghl_risk_acceptance, ghl_form_landings = _ghl_adapter(
            payload, events, all_offers
        )

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
            ghl_form_ids=ghl_form_ids,
            ghl_risk_acceptance=ghl_risk_acceptance,
            ghl_form_landings=ghl_form_landings,
            products=products,
            payment_slip_methods=payment_slip_methods,
        )
        for flow, enabled in manifest.flows.items():
            blockers = manifest.flow_blockers(flow)
            if enabled and blockers:
                raise ManifestError(
                    f"flujos.{flow} esta prendido pero " + "; ".join(blockers)
                )
        _validate_products_apart(manifest)
        # El binding v1 valida hosts, rutas y slugs con sus propias reglas: si el v2
        # no se puede traducir, el manifiesto no sirve para los caminos de hoy.
        try:
            manifest.to_commercial_ally_configs()
        except ValueError as exc:
            raise ManifestError(f"el manifiesto no produce un binding valido: {exc}") from exc
        return manifest


def _validate_products_apart(manifest: InstanceManifest) -> None:
    """Que el ruteo pueda separar los productos (paso 2c).

    Un evento de Hotmart se rutea por (producto, oferta), un formulario por su
    landing y por su pagina, y la base guarda un binding por funnel: nada de eso
    puede estar en dos productos. Dos productos con el mismo producto de Hotmart
    (ATT2 se vendio con ofertas del de ATT1) comparten hotlink y nombre, porque
    son los de Hotmart.
    """

    entries = [
        (
            manifest.funnel_ref,
            manifest.hotmart_product_id,
            manifest.hotlink,
            manifest.product_name,
            manifest.offers,
        )
    ] + [
        (
            product.commerce.funnel_ref,
            product.commerce.hotmart_product_id,
            product.commerce.hotlink,
            product.commerce.product_name,
            product.commerce.offers,
        )
        for product in manifest.products
        if product.commerce is not None
    ]
    if len(entries) == 1:
        return
    funnels = [entry[0] for entry in entries]
    if len(set(funnels)) != len(funnels):
        raise ManifestError("productos: dos productos con el mismo funnel_ref")
    hotmart_of: dict[int, tuple[str, str]] = {}
    owner: dict[tuple[object, ...], int] = {}
    for index, (_, product_id, hotlink, product_name, offers) in enumerate(entries):
        if hotmart_of.setdefault(product_id, (hotlink, product_name)) != (hotlink, product_name):
            raise ManifestError(
                f"productos: dos productos con el producto de Hotmart {product_id} y "
                "distinto hotlink o product_name (son los de Hotmart, iguales en los dos)"
            )
        for offer in offers:
            for key, what in (
                (("oferta", product_id, offer.code), f"la oferta {offer.code} de {product_id}"),
                (("landing", offer.site, offer.landing_id), f"la landing {offer.site}/{offer.landing_id}"),
                (("pagina", offer.page_host, offer.page_path), f"la pagina {offer.page_host}{offer.page_path}"),
            ):
                if owner.setdefault(key, index) != index:
                    raise ManifestError(f"productos: {what} esta en dos productos")


def _products(raw: object) -> tuple[Product, ...]:
    if raw is None:
        return ()
    if (
        not isinstance(raw, list)
        or not raw
        or not all(isinstance(entry, dict) for entry in raw)
    ):
        raise ManifestError("productos debe ser una lista de tablas ([[productos]])")
    if len(raw) > MAX_PRODUCTS:
        raise ManifestError(f"productos: admite hasta {MAX_PRODUCTS} productos")
    products = []
    for index, entry in enumerate(raw):
        where = f"productos[{index}]"
        if index == 0:
            above = [key for key in _PRINCIPAL_ONLY_ABOVE if key in entry]
            if above:
                raise ManifestError(
                    f"{where} es el producto principal: su {', '.join(above)} es el de "
                    "[instancia] y [hotmart]"
                )
            _exact_keys(where, entry, {"clave"}, {"etiqueta", "macro_etiqueta"})
        else:
            _exact_keys(
                where,
                entry,
                {"clave", *_PRINCIPAL_ONLY_ABOVE},
                {"etiqueta", "macro_etiqueta"},
            )
        product_ref = _ref(entry, "clave", f"{where}.clave")
        if ("etiqueta" in entry) != ("macro_etiqueta" in entry):
            raise ManifestError(f"{where}: etiqueta y macro_etiqueta van juntas")
        label = None
        macro_id = None
        if "etiqueta" in entry:
            label = _str(entry, "etiqueta", f"{where}.etiqueta")
            if _CHATWOOT_LABEL.fullmatch(label) is None:
                raise ManifestError(
                    f"{where}.etiqueta debe ser una etiqueta de Chatwoot en minusculas "
                    "(a-z, 0-9, guiones y guiones bajos)"
                )
            if label in RESERVED_CHATWOOT_LABELS:
                raise ManifestError(
                    f"{where}.etiqueta '{label}' es una etiqueta de la automatizacion"
                )
            macro_id = _positive_int(entry, "macro_etiqueta", f"{where}.macro_etiqueta")
        commerce = None
        if index:
            hotmart = entry["hotmart"]
            if not isinstance(hotmart, dict):
                raise ManifestError(f"{where}.hotmart debe ser una tabla")
            product_id, hotlink, product_name, currency, price, offers = _hotmart(
                hotmart, f"{where}.hotmart"
            )
            commerce = ProductCommerce(
                funnel_ref=_ref(entry, "funnel_ref", f"{where}.funnel_ref"),
                binding_version=_positive_int(
                    entry, "binding_version", f"{where}.binding_version"
                ),
                hotmart_product_id=product_id,
                hotlink=hotlink,
                product_name=product_name,
                currency=currency,
                price=price,
                offers=offers,
            )
        products.append(
            Product(
                key=product_ref,
                chatwoot_label=label,
                chatwoot_label_macro_id=macro_id,
                commerce=commerce,
            )
        )
    for what, values in (
        ("clave", [product.key for product in products]),
        ("etiqueta", [product.chatwoot_label for product in products if product.chatwoot_label]),
        (
            "macro_etiqueta",
            [
                product.chatwoot_label_macro_id
                for product in products
                if product.chatwoot_label_macro_id is not None
            ],
        ),
    ):
        if len(set(values)) != len(values):
            raise ManifestError(f"productos: dos productos con la misma {what}")
    return tuple(products)


def _hotmart(
    table: Mapping[str, Any], where: str
) -> tuple[int, str, str, str, Decimal, tuple[Offer, ...]]:
    """``[hotmart]`` del principal o ``[productos.hotmart]`` de otro producto."""

    _exact_keys(
        where,
        table,
        {"product_id", "hotlink", "product_name", "moneda", "precio", "ofertas"},
    )
    product_id = _positive_int(table, "product_id", f"{where}.product_id")
    hotlink = _str(table, "hotlink", f"{where}.hotlink")
    if _HOTLINK.fullmatch(hotlink) is None:
        raise ManifestError(f"{where}.hotlink debe ser alfanumerico")
    product_name = _str(table, "product_name", f"{where}.product_name")
    currency = _str(table, "moneda", f"{where}.moneda")
    if _CURRENCY.fullmatch(currency) is None:
        raise ManifestError(f"{where}.moneda debe ser un codigo de tres letras mayusculas")
    price = _price(table.get("precio"), f"{where}.precio")
    offers = _offers(table.get("ofertas"), f"{where}.ofertas")
    return product_id, hotlink, product_name, currency, price, offers


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


def _price(value: object, where: str = "hotmart.precio") -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ManifestError(f"{where} debe ser un texto decimal (\"47\") o un entero")
    try:
        price = Decimal(str(value))
    except InvalidOperation as exc:
        raise ManifestError(f"{where} no es un decimal") from exc
    if not price.is_finite() or price <= 0:
        raise ManifestError(f"{where} debe ser positivo")
    return price


def _str_list(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() == item and item for item in value
    ):
        raise ManifestError(f"{where} debe ser una lista de textos no vacios")
    if len(set(value)) != len(value):
        raise ManifestError(f"{where} tiene elementos repetidos")
    return tuple(value)


def _ghl_adapter(
    payload: Mapping[str, Any], events: frozenset[str], offers: tuple[Offer, ...]
) -> tuple[tuple[str, ...], GhlRiskAcceptance | None, GhlFormLandings | None]:
    """``[adaptadores.ghl]``: los formularios de GHL que entran como intencion,
    si esta, la aceptacion escrita del riesgo del adaptador, y si esta, en que
    landing vive cada formulario.

    Listar un formulario afirma que muestra la aclaracion de
    ``consentimiento.copy_version``: cada envio traducido sale con
    ``whatsapp_contact = true``.
    """

    if "adaptadores" not in payload:
        return (), None, None
    adapters = _table(payload, "adaptadores")
    _exact_keys("adaptadores", adapters, set(), {"ghl"})
    if "ghl" not in adapters:
        return (), None, None
    ghl = adapters["ghl"]
    if not isinstance(ghl, dict):
        raise ManifestError("adaptadores.ghl debe ser una tabla")
    _exact_keys(
        "adaptadores.ghl",
        ghl,
        {"formularios"},
        set(_GHL_RISK_KEYS) | set(_GHL_FORM_LANDING_KEYS),
    )
    forms = _str_list(ghl["formularios"], "adaptadores.ghl.formularios")
    if not forms:
        raise ManifestError("adaptadores.ghl.formularios debe tener al menos un formulario")
    for form in forms:
        if _GHL_FORM_ID.fullmatch(form) is None:
            raise ManifestError(
                f"adaptadores.ghl.formularios: '{form}' no es un id de formulario de GHL "
                "(20 letras o numeros)"
            )
    if "intencion" not in events:
        raise ManifestError("adaptadores.ghl exige el evento 'intencion' en eventos")
    return forms, _ghl_risk_acceptance(ghl), _ghl_form_landings(ghl, forms, offers)


def _ghl_form_landings(
    ghl: Mapping[str, Any], forms: tuple[str, ...], offers: tuple[Offer, ...]
) -> GhlFormLandings | None:
    """``landing_por_formulario`` y ``landing_por_formulario_modo``, las dos o
    ninguna.

    Cada formulario tiene que estar en ``formularios`` y cada landing tiene que
    ser el ``landing_id`` de una sola oferta: un error aca falla al cargar, en voz
    alta, y no en cada envio.
    """

    present = [key for key in _GHL_FORM_LANDING_KEYS if key in ghl]
    if not present:
        return None
    if len(present) != len(_GHL_FORM_LANDING_KEYS):
        raise ManifestError(
            "adaptadores.ghl: landing_por_formulario y landing_por_formulario_modo "
            "van juntas"
        )
    mode = _str(ghl, "landing_por_formulario_modo", "adaptadores.ghl.landing_por_formulario_modo")
    if mode not in GHL_FORM_LANDING_MODES:
        raise ManifestError(
            "adaptadores.ghl.landing_por_formulario_modo debe ser uno de "
            + ", ".join(GHL_FORM_LANDING_MODES)
        )
    raw = ghl["landing_por_formulario"]
    where = "adaptadores.ghl.landing_por_formulario"
    if not isinstance(raw, dict) or not raw:
        raise ManifestError(f"{where} debe ser una tabla {{ <formulario> = \"<landing_id>\" }} no vacia")
    landing_by_form = {}
    for form, landing_id in raw.items():
        if form not in forms:
            raise ManifestError(
                f"{where}: '{form}' no esta en adaptadores.ghl.formularios"
            )
        if not isinstance(landing_id, str):
            raise ManifestError(f"{where}.{form} debe ser el landing_id de una oferta")
        matches = [offer for offer in offers if offer.landing_id == landing_id]
        if not matches:
            raise ManifestError(
                f"{where}.{form}: '{landing_id}' no es el landing_id de ninguna oferta"
            )
        if len(matches) > 1:
            raise ManifestError(
                f"{where}.{form}: '{landing_id}' es el landing_id de mas de una oferta"
            )
        landing_by_form[form] = landing_id
    return GhlFormLandings(mode=mode, landing_by_form=MappingProxyType(landing_by_form))


def _utc_today() -> date:
    # Aparte para que las pruebas fijen el dia sin depender del reloj.
    return datetime.now(timezone.utc).date()


def _ghl_risk_acceptance(ghl: Mapping[str, Any]) -> GhlRiskAcceptance | None:
    """Las tres claves de la aceptacion, todas o ninguna.

    No hay una clave de estado: la presencia completa es la aceptacion. Este
    codigo solo la lee; la escribe a mano quien firma, en el manifiesto de la
    instancia.
    """

    present = [key for key in _GHL_RISK_KEYS if key in ghl]
    if not present:
        return None
    if len(present) != len(_GHL_RISK_KEYS):
        raise ManifestError(
            "adaptadores.ghl: la aceptacion del riesgo lleva "
            "riesgo_aceptado_por, riesgo_aceptado_el y riesgo_contrato"
        )
    accepted_by = _str(ghl, "riesgo_aceptado_por", "adaptadores.ghl.riesgo_aceptado_por")
    if any(char in accepted_by for char in _GHL_RISK_PLACEHOLDER_CHARS):
        raise ManifestError(
            "adaptadores.ghl.riesgo_aceptado_por es el marcador del ejemplo (<...>): "
            "lo escribe a mano quien decide, con su nombre"
        )
    accepted_on = ghl["riesgo_aceptado_el"]
    # Una fecha TOML (2026-10-02), no un texto ni una fecha con hora.
    if not isinstance(accepted_on, date) or hasattr(accepted_on, "hour"):
        raise ManifestError("adaptadores.ghl.riesgo_aceptado_el debe ser una fecha (2026-10-02)")
    # Es el dia en que se acepto. Un dia de margen sobre la fecha UTC: quien
    # firma al este de UTC ya esta en el dia siguiente.
    if accepted_on > _utc_today() + timedelta(days=1):
        raise ManifestError(
            "adaptadores.ghl.riesgo_aceptado_el no puede ser posterior a hoy: "
            "es el dia en que se acepto"
        )
    contract = _str(ghl, "riesgo_contrato", "adaptadores.ghl.riesgo_contrato")
    if contract != GHL_ADAPTER_RISK_CONTRACT:
        raise ManifestError(
            f"adaptadores.ghl.riesgo_contrato debe ser '{GHL_ADAPTER_RISK_CONTRACT}': "
            "la aceptacion es de otra version del contrato del adaptador"
        )
    return GhlRiskAcceptance(accepted_by=accepted_by, accepted_on=accepted_on, contract=contract)


def _offers(value: object, field: str = "hotmart.ofertas") -> tuple[Offer, ...]:
    if not isinstance(value, list) or not value:
        raise ManifestError(f"{field} debe tener al menos una oferta")
    offers = []
    for index, raw in enumerate(value):
        where = f"{field}[{index}]"
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
        raise ManifestError(f"{field} debe tener exactamente una oferta con por_defecto = true")
    codes = [offer.code for offer in offers]
    if len(set(codes)) != len(codes):
        raise ManifestError(f"{field} tiene codigos repetidos")
    landings = [(offer.site, offer.landing_id) for offer in offers]
    if len(set(landings)) != len(landings):
        raise ManifestError(f"{field} tiene dos ofertas para la misma landing")
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
        vocabulary = SLOT_TEMPLATE_PARAMETERS.get(slot, TEMPLATE_PARAMETERS)
        parameters = (
            _template_parameters(raw["parametros"], f"{where}.parametros", vocabulary)
            if "parametros" in raw
            else SLOT_DEFAULT_TEMPLATE_PARAMETERS.get(slot, DEFAULT_TEMPLATE_PARAMETERS)
        )
        templates[slot] = Template(name=name, language=language, parameters=parameters)
    return MappingProxyType(templates)


def _template_parameters(
    value: object,
    where: str,
    vocabulary: tuple[str, ...] = TEMPLATE_PARAMETERS,
) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= len(vocabulary):
        raise ManifestError(
            f"{where} debe ser una lista de 1 a {len(vocabulary)} variables"
        )
    unknown = [item for item in value if item not in vocabulary]
    if unknown:
        raise ManifestError(
            f"{where}: variables no soportadas {', '.join(map(str, unknown))} "
            f"(validas: {', '.join(vocabulary)})"
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
    _exact_keys("flujos", table, set(FLOWS), set(OPTIONAL_FLOWS))
    for flow, enabled in table.items():
        if type(enabled) is not bool:
            raise ManifestError(f"flujos.{flow} debe ser true o false")
    return MappingProxyType({
        flow: table[flow]
        for flow in FLOWS + OPTIONAL_FLOWS
        if flow in table
    })


def _payment_slip_methods(payload: Mapping[str, Any]) -> tuple[PaymentSlipMethod, ...]:
    """``[[ficha.medios]]``, validada por ``payment_slip.parse_payment_slip_methods``.

    Sin ``[ficha]`` no hay medios. Una fila mal escrita no carga: el bridge no
    arranca con una tabla que mandaria la ficha a otra base o con otra fecha.
    """

    if "ficha" not in payload:
        return ()
    ficha = _table(payload, "ficha")
    _exact_keys("ficha", ficha, {"medios"})
    try:
        return parse_payment_slip_methods(ficha["medios"])
    except ValueError as exc:
        raise ManifestError(str(exc)) from exc
