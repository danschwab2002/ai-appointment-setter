"""Las ofertas huerfanas de Hotmart: el registro (hotmart-unknown-offers-v1, E-565).

Un evento de Hotmart de un producto o una oferta que la instancia no declara se
perdia con un 200 y, en el mejor caso, una linea de log (E-422: 14 de 24 compras
del Black de ATT1 sin bienvenida). Con ``HOTMART_UNKNOWN_OFFERS_ENABLED=true`` el
bridge lo deja anotado en ``hotmart_unknown_offers`` desde los tres puntos de
entrada del contrato (§2):

- ``sin_ruta``: la instancia declara varios productos y ``for_hotmart`` no le
  encuentra ninguno (``HotmartUnrouted``).
- ``parser_descarta``: con una sola ruta, el parser rechaza un evento legible
  **porque** su producto o su oferta no son los del principal. Un payload roto
  de verdad (sin producto, sin transaccion, sin comprador...) no deja fila.
- ``despacho``: el despachador frena el envio de un flujo durable por una oferta
  que ningun producto declara (E-432, ``recipient_offer_not_declared``).

Nada de esto cambia la respuesta a Hotmart ni el camino del evento: el registro
es un efecto de costado y una falla queda en el log
(``hotmart_unknown_offer_record_failed``). Sin datos personales: el producto, la
oferta, el tipo de evento y el hash del ID del evento.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
import re
from typing import Protocol

from bridge.commercial_ally import CommercialAllyConfig
from bridge.hotmart import (
    EVENT_CART_ABANDONMENT,
    EVENT_PURCHASE_APPROVED,
    parse_hotmart_payload,
    parse_hotmart_purchase_payload,
)
from bridge.product_routes import (
    HOTMART_OFFER_NOT_DECLARED,
    HOTMART_PRODUCT_NOT_DECLARED,
    HotmartUnrouted,
)

logger = logging.getLogger(__name__)

UNKNOWN_OFFERS_ENV = "HOTMART_UNKNOWN_OFFERS_ENABLED"

ENTRY_UNROUTED = "sin_ruta"
ENTRY_PARSER_REJECTED = "parser_descarta"
ENTRY_DISPATCH = "despacho"
ENTRIES = frozenset({ENTRY_UNROUTED, ENTRY_PARSER_REJECTED, ENTRY_DISPATCH})
REASONS = frozenset({HOTMART_PRODUCT_NOT_DECLARED, HOTMART_OFFER_NOT_DECLARED})

# Los mismos de la migracion 20261114000300: lo que no tenga esta forma no se
# manda (un codigo de oferta raro no es del catalogo).
_OFFER_CODE = re.compile(r"[A-Za-z0-9]{1,40}")
_EVENT_TYPE = re.compile(r"[A-Z][A-Z_]{0,63}")


@dataclass(frozen=True)
class UnknownOffer:
    """Un evento huerfano, listo para el registro. Nada personal."""

    hotmart_product_id: int
    offer_code: str | None
    event_type: str | None
    motivo: str
    entrada: str
    event_ref_sha256: str
    source_event_id: str | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.hotmart_product_id, bool)
            or not isinstance(self.hotmart_product_id, int)
            or self.hotmart_product_id <= 0
        ):
            raise ValueError("invalid_unknown_offer_product")
        if self.offer_code is not None and _OFFER_CODE.fullmatch(self.offer_code) is None:
            raise ValueError("invalid_unknown_offer_code")
        if self.event_type is None and self.source_event_id is None:
            raise ValueError("invalid_unknown_offer_event_type")
        if self.event_type is not None and _EVENT_TYPE.fullmatch(self.event_type) is None:
            raise ValueError("invalid_unknown_offer_event_type")
        if self.motivo not in REASONS:
            raise ValueError("invalid_unknown_offer_reason")
        if self.entrada not in ENTRIES:
            raise ValueError("invalid_unknown_offer_entry")


def event_ref_sha256(kind: str, value: str) -> str:
    """El hash del evento que hace idempotente el conteo (contrato §4)."""

    return hashlib.sha256(f"{kind}:{value}".encode("utf-8")).hexdigest()


def _offer_code(value: object) -> str | None:
    return value if isinstance(value, str) and _OFFER_CODE.fullmatch(value) else None


def from_unrouted(
    unrouted: HotmartUnrouted, *, event_id: str, event_type: str
) -> UnknownOffer | None:
    """El punto 1: un evento sin ruta en una instancia de varios productos."""

    if unrouted.product_id is None or unrouted.product_id <= 0:
        return None
    if unrouted.reason not in REASONS or _EVENT_TYPE.fullmatch(event_type) is None:
        return None
    return UnknownOffer(
        hotmart_product_id=unrouted.product_id,
        offer_code=_offer_code(unrouted.offer_code),
        event_type=event_type,
        motivo=unrouted.reason,
        entrada=ENTRY_UNROUTED,
        event_ref_sha256=event_ref_sha256("hotmart_event", event_id),
    )


def from_parser_rejection(
    payload: object, *, config: CommercialAllyConfig, event_id: str, event_type: str
) -> UnknownOffer | None:
    """El punto 2: el parser con ``config`` descarto el evento. ¿Fue por el catalogo?

    Se vuelve a leer el payload sin el binding. Si tampoco se lee, es un payload
    roto de verdad y no es una oferta huerfana (None). Si se lee, el descarte fue
    porque el producto no es el del principal o, en un carrito, porque la oferta
    no esta entre las suyas. La compra frena con cualquier oferta del producto
    (``parse_hotmart_purchase_payload``), asi que ahi solo cuenta el producto.
    """

    if event_type == EVENT_CART_ABANDONMENT:
        cart = parse_hotmart_payload(payload, config=None)
        if cart is None:
            return None
        product_id, offer_code = cart.product_id, cart.offer_code
        if product_id != config.hotmart_product_id:
            reason = HOTMART_PRODUCT_NOT_DECLARED
        elif offer_code not in config.accepted_offer_codes:
            reason = HOTMART_OFFER_NOT_DECLARED
        else:
            return None
    elif event_type == EVENT_PURCHASE_APPROVED:
        purchase = parse_hotmart_purchase_payload(payload, config=None)
        if purchase is None or purchase.product_id == config.hotmart_product_id:
            return None
        product_id, offer_code = purchase.product_id, purchase.offer_code
        reason = HOTMART_PRODUCT_NOT_DECLARED
    else:
        return None
    if product_id <= 0:
        return None
    return UnknownOffer(
        hotmart_product_id=product_id,
        offer_code=_offer_code(offer_code),
        event_type=event_type,
        motivo=reason,
        entrada=ENTRY_PARSER_REJECTED,
        event_ref_sha256=event_ref_sha256("hotmart_event", event_id),
    )


def for_dispatch(
    *,
    offer_code: str,
    product_name: str,
    configs: list[CommercialAllyConfig],
    source_event_id: str,
) -> UnknownOffer | None:
    """El punto 3: el despachador freno una oferta que ningun producto declara.

    El producto de Hotmart es el del producto con ese nombre (el mismo criterio de
    ``_recipient_offer_not_declared``); el tipo del evento lo lee la base del
    evento que abrio el caso (``webhook_events``). Un mismo evento cuenta una vez
    aunque su caso tenga varios toques frenados.
    """

    product_ids = {
        config.hotmart_product_id
        for config in configs
        if config.product_name == product_name and config.hotmart_product_id > 0
    }
    if len(product_ids) != 1 or not source_event_id:
        return None
    return UnknownOffer(
        hotmart_product_id=next(iter(product_ids)),
        offer_code=_offer_code(offer_code),
        event_type=None,
        motivo=HOTMART_OFFER_NOT_DECLARED,
        entrada=ENTRY_DISPATCH,
        event_ref_sha256=event_ref_sha256("source_event", source_event_id),
        source_event_id=source_event_id,
    )


class UnknownOfferStore(Protocol):
    async def record_hotmart_unknown_offer(
        self, *, tenant_ref: str, offer: UnknownOffer
    ) -> str: ...

    async def list_hotmart_unknown_offers(
        self, *, tenant_ref: str, only_open: bool = True
    ) -> list[dict[str, object]]: ...


class HotmartUnknownOfferRecorder:
    """Anota una oferta huerfana sin frenar nunca el camino del evento."""

    def __init__(self, store: UnknownOfferStore, *, tenant_ref: str) -> None:
        if not isinstance(tenant_ref, str) or not tenant_ref:
            raise ValueError("invalid_unknown_offer_tenant")
        self._store = store
        self._tenant_ref = tenant_ref

    async def record(self, offer: UnknownOffer | None) -> None:
        if offer is None:
            return
        try:
            outcome = await self._store.record_hotmart_unknown_offer(
                tenant_ref=self._tenant_ref, offer=offer
            )
        except Exception as exc:  # noqa: BLE001 - el evento sigue su camino
            logger.warning(
                "hotmart_unknown_offer_record_failed entrada=%s error_type=%s",
                offer.entrada,
                type(exc).__name__,
            )
            return
        logger.info(
            "hotmart_unknown_offer_recorded entrada=%s motivo=%s product_id=%s offer=%s outcome=%s",
            offer.entrada,
            offer.motivo,
            offer.hotmart_product_id,
            offer.offer_code or "-",
            outcome,
        )

    async def open_count(self) -> int:
        """Cuantas ofertas siguen abiertas (para /ready). Las fallas suben."""

        rows = await self._store.list_hotmart_unknown_offers(
            tenant_ref=self._tenant_ref, only_open=True
        )
        return len(rows)
