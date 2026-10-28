"""El bridge del motor nativo (contrato flow-engine-v2, el PR 3 del hito 4, E-065).

Un flujo nativo nace en el motor, sin legado (la bienvenida despues de la
compra y, desde E-161, el carrito de un toque). Este modulo tiene las puntas
que el bridge le pone:

- **La compra va a la admision nativa** (``admit_native_purchase``), despues de
  que el worker la aplico y corto los casos abiertos. El worker la anota en la
  cola de la base antes de aplicarla; si la admision no contesta, la compra
  sigue en la cola y el lote la reintenta. El contacto se resuelve sin tocar el
  estado del evento: ese lo deja ``apply_hotmart_purchase_approved``.
- **El carrito va a la admision nativa antes que al legado**
  (``admit_native_cart``, flow-engine-v2 2.7.0): lo llama el resolver con el
  contacto ya resuelto; si un flujo nativo lo toma, el legado no planifica.
- **El paso ``etiquetar: cliente``** (``apply_native_tail``), despues de que la
  plantilla del paso quedo aceptada: la etiqueta ``cliente`` por un macro que
  solo la suma (el patron de ``ProductLabeler``) y una nota privada con el
  producto y la fecha. Falla blanda: el envio ya salio y no se deshace.

Nada de esto corre sin ``FLOW_ENGINE_ENABLED`` y sin un flujo nativo prendido o
en cohorte. Los logs no llevan telefono, mail ni nombre.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from bridge.commercial_ally import CommercialAllyConfig
from bridge.hotmart import HotmartPurchaseData, parse_hotmart_purchase_payload
from bridge.phones import equivalent_whatsapp_phones
from bridge.product_routes import HotmartUnrouted, ProductRoutes
from bridge.resolution import _stored_whatsapp_identity
from bridge.supabase import (
    FlowEngineAdmissionUnavailable,
    SupabaseClient,
    SupabaseError,
    SupabasePermanentError,
)

if TYPE_CHECKING:
    from bridge.flow_engine_runtime import FlowEngineRuntime

logger = logging.getLogger(__name__)

# El ancla de la accion de un recorrido nativo (20261024000200): la pone la
# admision nativa, y ninguna otra accion la tiene.
NATIVE_ANCHOR = "flow_engine_event"
NATIVE_PURCHASE_TRIGGER = "compra"
# El carrito abandonado (E-161, flow-engine-v2 2.7.0): entra con permiso formulario.
NATIVE_CART_TRIGGER = "carrito"
# Una accion nativa solo sale con la plantilla del paso, en el modo directo y
# con el motor prendido: nunca por Hermes ni con la plantilla del manifiesto.
NATIVE_NEEDS_DIRECT_TEMPLATE = "flow_engine_native_needs_direct_template"
NATIVE_NEEDS_ENGINE = "flow_engine_native_needs_engine"
NATIVE_STEP_CONTEXT_UNAVAILABLE = "flow_engine_native_step_context_unavailable"
# La categoria de la plantilla de un paso nativo sale del permiso del flujo
# (flow-engine-v2 2.6.0): con ``permiso: compra`` solo utilidad (regla 17 del
# esquema: una sola plantilla de utilidad); con cualquier otro permiso, la de
# marketing tambien. Desde 20261028000200 (E-161) el contexto del paso trae el
# permiso de la version fijada (``entry_permission``) y el envio lo usa con
# ``native_step_categories``; sin el dato, el de ``NATIVE_PURCHASE_PERMISSION``.
NATIVE_PURCHASE_PERMISSION = "compra"
NATIVE_UTILITY_CATEGORY = "UTILITY"
NATIVE_MARKETING_CATEGORY = "MARKETING"


def native_template_categories(permission: object) -> frozenset[str]:
    """Las categorias que acepta el envio de un paso nativo con ese permiso (2.6.0)."""
    if permission == NATIVE_PURCHASE_PERMISSION:
        return frozenset({NATIVE_UTILITY_CATEGORY})
    return frozenset({NATIVE_MARKETING_CATEGORY, NATIVE_UTILITY_CATEGORY})


# Lo que acepta el envio de un paso nativo de la compra, y el de uno cuyo permiso no llego.
NATIVE_PURCHASE_TEMPLATE_CATEGORIES = native_template_categories(NATIVE_PURCHASE_PERMISSION)


def native_step_categories(entry_permission: str | None) -> frozenset[str]:
    """Las categorias del envio de un paso nativo, con el permiso de su version fijada (E-161).

    El contexto del paso trae ``entry_permission`` desde 20261028000200. Sin el (una base sin la
    migracion), el envio sigue como antes: solo utilidad. Nunca abre ``MARKETING`` por falta del dato.
    """
    if not isinstance(entry_permission, str) or not entry_permission:
        return NATIVE_PURCHASE_TEMPLATE_CATEGORIES
    return native_template_categories(entry_permission)


# Lo que devuelve ``admit_native_purchase`` (E-165): la admision contesto y un flujo arranco
# (``admitida``), contesto sin arrancar ninguno (``sin_arranque``: fuera de la cohorte, la persona
# no entra o no hay flujo), o no contesto (``reintentar``). ``descartada:<motivo>`` nunca puede.
NATIVE_PURCHASE_STARTED = "admitida"
NATIVE_PURCHASE_NOT_STARTED = "sin_arranque"
NATIVE_PURCHASE_RETRY = "reintentar"
NATIVE_STEP_VARIABLES = frozenset({"nombre", "producto"})
NATIVE_QUEUE_PAGE = 5
CLIENT_LABEL = "cliente"
_OFFER = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


class NativePurchaseSkipped(RuntimeError):
    """The purchase cannot go to the native admission, now or ever."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def purchase_product_ref(
    payload: Mapping[str, object],
    *,
    commercial_ally_config: CommercialAllyConfig | None,
    product_routes: ProductRoutes | None,
) -> str | None:
    """The product of the instance a purchase belongs to, by Hotmart id.

    Never by name (choque 7 del diseno): a bump or an upsell of another product
    is its own ``funnel_ref``, and a product the instance does not declare has
    none.
    """
    if product_routes is not None:
        route = product_routes.for_hotmart(payload)
        return None if isinstance(route, HotmartUnrouted) else route.config.funnel_ref
    if commercial_ally_config is None:
        return None
    purchase = parse_hotmart_purchase_payload(payload)
    if purchase is None or purchase.product_id != commercial_ally_config.hotmart_product_id:
        return None
    return commercial_ally_config.funnel_ref


def cart_product_ref(
    payload: Mapping[str, object],
    *,
    commercial_ally_config: CommercialAllyConfig | None,
    product_routes: ProductRoutes | None,
) -> str | None:
    """The product of the instance a cart belongs to, by Hotmart id (E-161).

    Like ``purchase_product_ref``, for a ``PURCHASE_OUT_OF_SHOPPING_CART``
    payload: with several products, its route (product and offer); with one,
    the product id of the payload against the instance.
    """
    if product_routes is not None:
        route = product_routes.for_hotmart(payload)
        return None if isinstance(route, HotmartUnrouted) else route.config.funnel_ref
    if commercial_ally_config is None:
        return None
    data = payload.get("data")
    product = data.get("product") if isinstance(data, Mapping) else None
    product_id = product.get("id") if isinstance(product, Mapping) else None
    if type(product_id) is not int or product_id != commercial_ally_config.hotmart_product_id:
        return None
    return commercial_ally_config.funnel_ref


# What a native cart admission leaves to the resolver (E-161, flow-engine-v2 2.7.0).
NATIVE_CART_STARTED = "arranco"          # a native run owns the event: no legacy planner
NATIVE_CART_REFUSED = "no_arranca"       # a native flow took the person and did not start: no legacy
NATIVE_CART_UNCLAIMED = "sin_reclamo"    # no native flow takes the person: the legacy plans as today
NATIVE_CART_PENDING = "pendiente"        # the admission did not answer: the event stays received
# Not taking the person: outside the cohort, or the flow went off or paused after the read.
NATIVE_CART_UNCLAIMED_REASONS = frozenset({"fuera_de_cohorte", "flujo_apagado", "flujo_pausado"})


@dataclass(frozen=True)
class NativeCartAdmission:
    outcome: str
    recovery_case_id: str | None = None
    reason: str | None = None


async def admit_native_cart(
    supabase: SupabaseClient,
    *,
    runtime: FlowEngineRuntime,
    webhook_event_id: str,
    product_ref: str,
    contact_id: str,
    offer_code: str | None,
    moment: str,
    chatwoot_account_id: int,
    chatwoot_inbox_id: int,
    destination: str,
) -> NativeCartAdmission:
    """Admit a cart to the native cart flows of its product, before the legacy planner.

    One event opens one case (``recovery_cases.abandonment_event_id`` is
    unique), so the native admission goes first and the legacy only plans what
    no native flow takes: ``sin_reclamo`` (``sin_flujo``, outside the cohort, or
    a flow off or paused). ``arranco`` hands the native case to the resolver;
    ``no_arranca`` (the person is the flow's and did not enter) ends the event
    without the legacy, like the cart of the engine (``motor_no_arranca``).
    A call that did not answer is ``pendiente`` (the event stays received and
    the batch tries again) up to the third consecutive failure; the fourth call
    still asks (a lost answer may have left the case created), and if it fails
    too, the legacy plans. A deterministic rejection goes to the legacy. Never
    raises.
    """
    failures = runtime.native_admission_failures
    # Desde el cuarto intento seguido sin respuesta se le pregunta igual (una respuesta perdida
    # pudo dejar el caso creado, y la repeticion lo devuelve), pero si vuelve a fallar planifica
    # el legado en vez de dejar el evento esperando.
    forced = failures.should_force_legacy(webhook_event_id)
    flow_event: dict[str, object] = {
        "fuente": "hotmart",
        "tipo": NATIVE_CART_TRIGGER,
        "evento_id": webhook_event_id,
        "producto_ref": product_ref,
        "oferta": offer_code
        if offer_code and len(offer_code) <= 64 and set(offer_code) <= _OFFER
        else None,
        "contacto_id": contact_id,
        "momento": moment,
        "chatwoot": {"account_id": chatwoot_account_id, "inbox_id": chatwoot_inbox_id},
        "destino": destination,
    }
    try:
        rows = await supabase.admit_flow_engine_event(runtime.instance, flow_event)
    except SupabasePermanentError as exc:
        logger.warning(
            "flow_engine_native_cart_rejected event_id=%s reason=%s",
            webhook_event_id,
            exc.reason,
        )
        return NativeCartAdmission(NATIVE_CART_UNCLAIMED, reason=exc.reason)
    except SupabaseError as exc:
        count = failures.record_failure(webhook_event_id)
        logger.warning(
            "flow_engine_native_cart_unavailable event_id=%s failures=%s forced=%s error=%s",
            webhook_event_id,
            count,
            forced,
            type(exc).__name__,
        )
        if forced:
            failures.clear(webhook_event_id)
            return NativeCartAdmission(NATIVE_CART_UNCLAIMED, reason="forzado_al_legado")
        return NativeCartAdmission(NATIVE_CART_PENDING, reason="admision_sin_respuesta")
    failures.clear(webhook_event_id)
    for row in rows:
        logger.info(
            "flow_engine_native_admission event_id=%s flow_id=%s outcome=%s reason=%s",
            webhook_event_id,
            row.flow_id,
            row.outcome,
            row.reason,
        )
    for row in rows:
        if row.outcome == "motor_arranco" and row.recovery_case_id is not None:
            return NativeCartAdmission(NATIVE_CART_STARTED, recovery_case_id=row.recovery_case_id)
    for row in rows:
        if row.outcome == "motor_no_arranca" and row.reason not in NATIVE_CART_UNCLAIMED_REASONS:
            return NativeCartAdmission(NATIVE_CART_REFUSED, reason=row.reason)
    return NativeCartAdmission(NATIVE_CART_UNCLAIMED)


async def resolve_purchase_contact(
    supabase: SupabaseClient,
    *,
    purchase: HotmartPurchaseData,
    webhook_event_id: str,
    commercial_ally_config: CommercialAllyConfig,
) -> str:
    """The contact of the buyer: found by email and phone, or created.

    The same lookups as the cart (``resolution.resolve_event``), without its
    status writes: the event of a purchase belongs to
    ``apply_hotmart_purchase_approved``. A buyer whose email and phone are two
    contacts is skipped for good (``identity_ambiguous``). A failed call raises
    ``SupabaseError`` and the purchase is tried again from the queue.
    """
    email_match = None
    phone_match = None
    try:
        if purchase.buyer_email is not None:
            email_match = await supabase.find_contact_by_email(purchase.buyer_email)
        if purchase.buyer_phone is not None:
            phone_match = await supabase.find_contact_by_phones(
                equivalent_whatsapp_phones(purchase.buyer_phone)
            )
    except SupabaseError as exc:
        if str(exc).endswith("_ambiguous"):
            raise NativePurchaseSkipped("identity_ambiguous") from exc
        raise
    if (
        email_match is not None
        and phone_match is not None
        and email_match.contact_id != phone_match.contact_id
    ):
        raise NativePurchaseSkipped("identity_ambiguous")
    match = email_match or phone_match
    if match is not None:
        contact_id = match.contact_id
    else:
        contact_id = await supabase.create_contact(
            full_name=purchase.buyer_name,
            email=purchase.buyer_email,
            phone=purchase.buyer_phone,
            country_iso=None,
        )
    for point_type, value in (("email", purchase.buyer_email), ("phone", purchase.buyer_phone)):
        if value is None:
            continue
        await supabase.create_contact_point(
            contact_id=contact_id,
            point_type=point_type,
            raw_value=value,
            normalized_value=value,
            source="hotmart",
            source_event_id=webhook_event_id,
        )
    return contact_id


def _moment(purchase: HotmartPurchaseData) -> str:
    # El disparo es la creation_date del webhook (contrato, 4.1).
    return datetime.fromtimestamp(purchase.creation_date_ms / 1000, tz=UTC).isoformat()


async def admit_native_purchase(
    supabase: SupabaseClient,
    *,
    runtime: FlowEngineRuntime,
    webhook_event_id: str,
    payload: Mapping[str, object],
    product_ref: str,
    commercial_ally_config: CommercialAllyConfig,
    now: str,
) -> str:
    """Admit one applied purchase to its native flows and close it in the queue.

    Returns what happened: ``admitida`` (the admission answered and a native
    flow started for the buyer, ``motor_arranco``), ``sin_arranque`` (it
    answered and none started: outside the cohort, the person does not enter,
    or no flow), ``descartada:<motivo>`` (it never can, the queue closes it) or
    ``reintentar`` (the admission or a lookup did not answer: the queue keeps
    it and counts the attempt). The worker pauses the agent for every outcome
    but ``admitida`` and ``reintentar`` (E-165). Never raises.
    """
    try:
        purchase = parse_hotmart_purchase_payload(payload)
        if purchase is None:
            raise NativePurchaseSkipped("invalid_purchase_payload")
        if purchase.buyer_phone is None:
            raise NativePurchaseSkipped("sin_telefono")
        contact_id = await resolve_purchase_contact(
            supabase,
            purchase=purchase,
            webhook_event_id=webhook_event_id,
            commercial_ally_config=commercial_ally_config,
        )
        destination = await _stored_whatsapp_identity(
            supabase,
            contact_id=contact_id,
            phone=purchase.buyer_phone,
            chatwoot_account_id=commercial_ally_config.chatwoot_account_id,
            chatwoot_inbox_id=commercial_ally_config.chatwoot_inbox_id,
        )
        offer = purchase.offer_code
        flow_event: dict[str, object] = {
            "fuente": "hotmart",
            "tipo": NATIVE_PURCHASE_TRIGGER,
            "evento_id": webhook_event_id,
            "producto_ref": product_ref,
            "oferta": offer if offer and len(offer) <= 64 and set(offer) <= _OFFER else None,
            "contacto_id": contact_id,
            "momento": _moment(purchase),
            "chatwoot": {
                "account_id": commercial_ally_config.chatwoot_account_id,
                "inbox_id": commercial_ally_config.chatwoot_inbox_id,
            },
            "destino": destination,
        }
        rows = await supabase.admit_flow_engine_event(runtime.instance, flow_event)
    except NativePurchaseSkipped as exc:
        outcome = f"descartada:{exc.reason}"
        done = True
    except SupabasePermanentError as exc:
        outcome = f"descartada:{exc.reason or 'rechazada'}"
        done = True
    except (FlowEngineAdmissionUnavailable, SupabaseError) as exc:
        logger.warning(
            "flow_engine_native_admission_unavailable event_id=%s error=%s",
            webhook_event_id,
            type(exc).__name__,
        )
        outcome = NATIVE_PURCHASE_RETRY
        done = False
    else:
        for row in rows:
            logger.info(
                "flow_engine_native_admission event_id=%s flow_id=%s outcome=%s reason=%s",
                webhook_event_id,
                row.flow_id,
                row.outcome,
                row.reason,
            )
        started = any(row.outcome == "motor_arranco" for row in rows)
        outcome = NATIVE_PURCHASE_STARTED if started else NATIVE_PURCHASE_NOT_STARTED
        done = True
    if outcome.startswith("descartada"):
        logger.warning(
            "flow_engine_native_purchase_skipped event_id=%s reason=%s",
            webhook_event_id,
            outcome.removeprefix("descartada:"),
        )
    try:
        await supabase.finish_flow_engine_native_event(
            webhook_event_id=webhook_event_id, done=done, now=now
        )
    except SupabaseError as exc:
        # Lo cerrado vuelve a leerse y la admision es idempotente por evento:
        # la proxima vuelta devuelve el mismo caso.
        logger.warning(
            "flow_engine_native_queue_mark_failed event_id=%s error=%s",
            webhook_event_id,
            type(exc).__name__,
        )
    return outcome


class _Labeler(Protocol):
    @property
    def label(self) -> str: ...

    async def ensure(self, conversation_id: int, *, source: str) -> str: ...


class _NoteClient(Protocol):
    async def ensure_marked_private_note(
        self,
        *,
        conversation_id: int,
        expected_inbox_id: int,
        note_body: str,
        idempotency_marker: str,
    ) -> bool: ...


def client_note(*, producto: str | None, momento: str | None) -> str:
    """The private note of ``etiquetar: cliente``: product and date, nothing else."""
    day = momento[:10] if isinstance(momento, str) and len(momento) >= 10 else "sin fecha"
    product = producto.strip() if isinstance(producto, str) and producto.strip() else "el producto"
    return f"Clienta: compró {product} el {day} (UTC). Lo marcó el flujo de la compra."


async def apply_native_tail(
    supabase: SupabaseClient,
    *,
    action_id: str,
    conversation_id: int,
    inbox_id: int,
    labeler: _Labeler | None,
    notes: _NoteClient,
) -> str:
    """Run what follows the accepted send of a native step. Never raises.

    ``sin_cola``: not a native step, or its version has no ``etiquetar``.
    ``hecho``: label and note. ``nota_sin_etiqueta``: the note, without the
    macro of the label configured or with it failing. ``fallo``: neither.
    """
    try:
        tail = await supabase.get_flow_engine_native_step_tail(action_id)
    except SupabaseError as exc:
        logger.warning(
            "flow_engine_native_tail_unavailable action_id=%s error=%s",
            action_id,
            type(exc).__name__,
        )
        return "fallo"
    if tail is None or tail.etiquetar != CLIENT_LABEL:
        return "sin_cola"
    label_outcome = "sin_macro"
    if labeler is not None:
        label_outcome = await labeler.ensure(conversation_id, source="flow_engine_native")
    try:
        await notes.ensure_marked_private_note(
            conversation_id=conversation_id,
            expected_inbox_id=inbox_id,
            note_body=client_note(producto=tail.producto, momento=tail.momento),
            idempotency_marker=f"[supportmagician-flujo:cliente:{action_id}]",
        )
        noted = True
    except Exception as exc:
        noted = False
        logger.warning(
            "flow_engine_native_note_failed action_id=%s conversation=%s error=%s",
            action_id,
            conversation_id,
            type(exc).__name__,
        )
    labeled = label_outcome in {"added", "present"}
    outcome = "hecho" if labeled and noted else "nota_sin_etiqueta" if noted else "fallo"
    logger.info(
        "flow_engine_native_tail action_id=%s conversation=%s label=%s note=%s outcome=%s",
        action_id,
        conversation_id,
        label_outcome,
        noted,
        outcome,
    )
    return outcome
