"""Las rutas por producto: que binding atiende a cada producto de la aliada.

Paso 2c de varios productos por aliada (decision de Dan del 2026-10-16: un
``funnel_ref`` por producto, con su binding, en la misma base). Es el punto unico
donde el bridge elige el producto: nada que dependa del producto deberia leer
``settings.commercial_ally_config`` a secas, que es solo el principal.

Con un solo producto (Johanna, o una instancia sin ``[[productos]]`` o con uno
solo) hay una sola ruta, el principal, y todo da lo mismo que antes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Mapping

from .commercial_ally import CommercialAllyConfig
from .hotmart import EVENT_CART_ABANDONMENT, EVENT_PURCHASE_APPROVED
from .instance_manifest import InstanceManifest

# Con mas de un producto el bridge no arranca hasta que el ruteo este completo:
# los eventos de Hotmart y los formularios (2c-2) y el link, la etiqueta y lo
# que hizo la persona (2c-3). Asi ningun manifiesto con dos productos corre con
# el ruteo a medias. La 2c-3 lo prende.
ROUTING_COMPLETE = False


# Por que un evento de Hotmart no tiene producto en la instancia.
HOTMART_PRODUCT_NOT_DECLARED = "product_not_declared"
HOTMART_OFFER_NOT_DECLARED = "offer_not_declared"


@dataclass(frozen=True)
class HotmartUnrouted:
    """Un evento de Hotmart que no es de ningun producto de la instancia.

    ``product_id`` y ``offer_code`` son los del evento (nada personal): van al log
    para que el descarte se vea. Antes de varios productos, un evento de otro
    producto salia con 200 y sin rastro.
    """

    reason: str
    product_id: int | None
    offer_code: str | None


def _hotmart_product_and_offer(payload: object) -> tuple[str | None, int | None, str | None]:
    """El tipo, el producto y la oferta de un evento de Hotmart v2.

    El carrito trae la oferta en ``data.offer.code``; la compra, el pago fallido y
    la ficha, en ``data.purchase.offer.code`` (medido en att1-db el 2026-10-16).
    """

    event = payload if isinstance(payload, Mapping) else {}
    event_type = event.get("event") if isinstance(event.get("event"), str) else None
    data = event.get("data") if isinstance(event.get("data"), Mapping) else {}
    product = data.get("product") if isinstance(data.get("product"), Mapping) else {}
    product_id = product.get("id")
    if isinstance(product_id, bool) or not isinstance(product_id, int):
        product_id = None
    if event_type == EVENT_CART_ABANDONMENT:
        holder = data
    else:
        holder = data.get("purchase") if isinstance(data.get("purchase"), Mapping) else {}
    offer = holder.get("offer") if isinstance(holder.get("offer"), Mapping) else {}
    offer_code = offer.get("code") if isinstance(offer.get("code"), str) else None
    return event_type, product_id, offer_code


@dataclass(frozen=True)
class ProductRoute:
    """Un producto y su binding.

    ``key`` es la ``clave`` de ``[[productos]]``; ``None`` en una instancia que no
    declara productos. ``principal`` es el que sale de ``[hotmart]`` y de
    ``[instancia]``: lo que es de la instancia (Chatwoot, el scope entrante, los
    flujos de hoy) sigue siendo suyo.
    """

    key: str | None
    config: CommercialAllyConfig
    principal: bool


class ProductRoutes:
    """Las rutas de la instancia: el principal primero, despues los demas."""

    def __init__(self, routes: tuple[ProductRoute, ...]) -> None:
        if not routes or not routes[0].principal:
            raise ValueError("product routes need the principal first")
        if sum(route.principal for route in routes) != 1:
            raise ValueError("product routes need exactly one principal")
        funnels = [route.config.funnel_ref for route in routes]
        if len(set(funnels)) != len(funnels):
            raise ValueError("two product routes share a funnel_ref")
        keys = [route.key for route in routes]
        if len(routes) > 1 and (None in keys or len(set(keys)) != len(keys)):
            raise ValueError("with more than one product every route needs its own key")
        self._routes = routes

    @classmethod
    def single(cls, config: CommercialAllyConfig, key: str | None = None) -> ProductRoutes:
        return cls((ProductRoute(key=key, config=config, principal=True),))

    @classmethod
    def build(
        cls,
        principal_config: CommercialAllyConfig,
        manifest: InstanceManifest | None,
    ) -> ProductRoutes:
        """Las rutas de la instancia que corre.

        El principal es ``principal_config``, el binding que ya carga el bridge
        (``settings.commercial_ally_config``). Los demas salen del manifiesto, en
        el orden de ``[[productos]]``.
        """

        if manifest is None or not manifest.products:
            return cls.single(principal_config)
        extras = manifest.to_commercial_ally_configs()[1:]
        products = [product for product in manifest.products[1:]]
        return cls(
            (ProductRoute(key=manifest.products[0].key, config=principal_config, principal=True),)
            + tuple(
                ProductRoute(key=product.key, config=config, principal=False)
                for product, config in zip(products, extras, strict=True)
            )
        )

    @property
    def principal(self) -> ProductRoute:
        return self._routes[0]

    def __iter__(self) -> Iterator[ProductRoute]:
        return iter(self._routes)

    def __len__(self) -> int:
        return len(self._routes)

    def by_key(self, key: str) -> ProductRoute | None:
        return next((route for route in self._routes if route.key == key), None)

    def by_funnel(self, funnel_ref: str) -> ProductRoute | None:
        return next(
            (route for route in self._routes if route.config.funnel_ref == funnel_ref),
            None,
        )

    def for_hotmart(self, payload: object) -> ProductRoute | HotmartUnrouted:
        """El producto de un evento de Hotmart, por producto y oferta (paso 2c-2).

        - Con una sola ruta, siempre el principal: los parsers y las RPC rechazan
          lo ajeno como antes, y nada cambia.
        - Un payload sin producto valido va al principal, para que el parser lo
          rechace como hoy.
        - (producto, oferta) declarado: ese producto. El manifiesto garantiza que
          es uno solo.
        - La compra con una oferta que ningun producto declara (la 83utgyow de la
          recuperacion de GHL en ATT1) va al primer producto con ese producto de
          Hotmart, el principal si es uno de ellos: una compra frena con
          cualquier oferta del producto, como antes.
        - Carrito, pago fallido o ficha con una oferta no declarada, o un
          producto que la instancia no tiene: sin ruta.
        """

        if len(self._routes) == 1:
            return self.principal
        event_type, product_id, offer_code = _hotmart_product_and_offer(payload)
        if product_id is None:
            return self.principal
        candidates = [
            route for route in self._routes if route.config.hotmart_product_id == product_id
        ]
        if not candidates:
            return HotmartUnrouted(HOTMART_PRODUCT_NOT_DECLARED, product_id, offer_code)
        for route in candidates:
            if offer_code in route.config.accepted_offer_codes:
                return route
        if event_type == EVENT_PURCHASE_APPROVED:
            return candidates[0]
        return HotmartUnrouted(HOTMART_OFFER_NOT_DECLARED, product_id, offer_code)

    def for_landing(self, site: object, landing_id: object) -> ProductRoute | None:
        """El producto de una landing (``site``/``landing_id``), o None."""

        if not isinstance(site, str) or not isinstance(landing_id, str):
            return None
        return next(
            (
                route
                for route in self._routes
                if route.config.offer_landing(site, landing_id) is not None
            ),
            None,
        )

    def owner_of_landing_id(self, landing_id: str) -> ProductRoute | None:
        """El producto que tiene una landing con ese ``landing_id``, si es uno solo."""

        owners = [
            route
            for route in self._routes
            if any(landing.landing_id == landing_id for landing in route.config.offer_landings)
        ]
        return owners[0] if len(owners) == 1 else None
