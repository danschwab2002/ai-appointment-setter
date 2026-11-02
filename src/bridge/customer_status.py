"""Si la persona ya es cliente, en la entrada del agente (E-297, P-11).

Diseno: ``documentacion/2026-11-02-e278-diseno-el-agente-sabe-que-es-cliente.md``
(E-278, PR 3) y las decisiones de Dan del 02/11 (D1 a D12). Contrato:
``docs/contracts/customer-status-v1.md``.

Con ``AGENT_CUSTOMER_STATUS_ENABLED``, en cada turno del agente el bridge lee
``get_customer_status_v1`` (migracion 20261103000700) y deja en la entrada
``customer_status``: una lista con cada producto que la persona compro, su
estado y desde cuando. Viaja en toda aliada con manifiesto, declare
``[[productos]]`` o no (D1); sin ``[[productos]]`` el producto es el nombre de
``[hotmart]``.

Las fuentes van en el orden de D2. La base ve tres (el link de esta
conversacion, el telefono y el mail del contacto); la cuarta, la etiqueta
``cliente`` puesta a mano en Chatwoot (o por el paso ``etiquetar: cliente`` de
un flujo, que es la misma), la suma este modulo desde la conversacion del
webhook. Es la salida del equipo para quien compro con otros datos (D7).

Lo que hace cumplir el bridge, sin depender del modelo:

* el link del producto que ya compro no sale: el turno se deriva con
  ``customer_link_request`` (D6);
* el cupon de recuperacion no se le ofrece ni se renueva por respuesta (D4, D5).

Este modulo es la parte pura: arma la entrada y contesta las preguntas del
freno. Lo impuro (la RPC) vive en ``bridge.supabase`` y el cableado en
``bridge.app``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from .product_routes import ProductRoute, ProductRoutes
from .supabase import CustomerStatus

CUSTOMER_STATUS_CONTEXT_KEY = "customer_status"
# La etiqueta de Chatwoot: la misma que pone el paso ``etiquetar`` de un flujo
# nativo (``native_flows.CLIENT_LABEL``) y la que pone una persona a mano.
CUSTOMER_LABEL = "cliente"
CUSTOMER_LABEL_SOURCE = "etiqueta"
# El motivo de la derivacion cuando el agente pide el link de lo que ya compro.
CUSTOMER_LINK_REQUEST_REASON = "customer_link_request"


def product_label(route: ProductRoute) -> str:
    """Como se llama el producto en la entrada: la clave de ``[[productos]]`` o,
    sin productos declarados, el nombre de ``[hotmart]``."""

    return route.key if route.key is not None else route.config.product_name


def conversation_labels(payload: Mapping[str, object]) -> tuple[str, ...]:
    """Las etiquetas de la conversacion del webhook ``message_created``.

    Chatwoot las manda en ``conversation.labels`` (captura
    ``chatwoot_message_created_inbox_9_conv_158_20260923.json``). Cualquier otra
    forma es ninguna etiqueta.
    """

    conversation = payload.get("conversation")
    if not isinstance(conversation, Mapping):
        return ()
    labels = conversation.get("labels")
    if not isinstance(labels, list):
        return ()
    return tuple(label for label in labels if isinstance(label, str))


def customer_status_entries(
    statuses: Iterable[CustomerStatus],
    *,
    routes: ProductRoutes,
    labels: Sequence[str],
) -> list[dict[str, str]]:
    """La entrada ``customer_status``: un item por producto comprado.

    ``product``, ``status`` y ``since`` (la primera compra vigente, en UTC) y
    ``source``. La etiqueta ``cliente`` marca como clienta del producto
    principal, el de los flujos de hoy, si la base no la vio. Un funnel que la
    instancia no tiene se descarta.
    """

    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for status in statuses:
        route = routes.by_funnel(status.funnel_ref)
        if route is None:
            continue
        product = product_label(route)
        if product in seen:
            continue
        seen.add(product)
        entries.append(
            {
                "product": product,
                "status": status.status,
                "since": status.first_purchase_at.isoformat(),
                "source": status.source,
            }
        )
    principal = product_label(routes.principal)
    if CUSTOMER_LABEL in labels and principal not in seen:
        entries.insert(
            0,
            {"product": principal, "status": "cliente", "source": CUSTOMER_LABEL_SOURCE},
        )
    return entries


def customer_entry_for(
    context: Mapping[str, object],
    *,
    route: ProductRoute,
) -> dict[str, object] | None:
    """El item de ``customer_status`` del producto de esa ruta, o None.

    Cualquier estado cuenta: a una clienta no se le manda el link de lo que ya
    tiene, y a una reembolsada o con contracargo que pide comprar otra vez la
    atiende una persona (D4).
    """

    entries = context.get(CUSTOMER_STATUS_CONTEXT_KEY)
    if not isinstance(entries, list):
        return None
    wanted = product_label(route)
    for entry in entries:
        if isinstance(entry, dict) and entry.get("product") == wanted:
            return entry
    return None


def is_customer_of_anything(context: Mapping[str, object]) -> bool:
    """Si la persona compro algun producto de la aliada (para el cupon, D5)."""

    entries = context.get(CUSTOMER_STATUS_CONTEXT_KEY)
    return isinstance(entries, list) and len(entries) > 0
