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
from typing import Iterator

from .commercial_ally import CommercialAllyConfig
from .instance_manifest import InstanceManifest

# Con mas de un producto el bridge no arranca hasta que el ruteo este completo:
# los eventos de Hotmart y los formularios (2c-2) y el link, la etiqueta y lo
# que hizo la persona (2c-3). Asi ningun manifiesto con dos productos corre con
# el ruteo a medias. La 2c-3 lo prende.
ROUTING_COMPLETE = False


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
