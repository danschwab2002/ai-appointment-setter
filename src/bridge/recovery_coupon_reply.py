"""La respuesta escrita a A2 reactiva el cupon, y el agente sabe cual es.

E-267, el arreglo de fondo de F-148 (diagnostico:
``documentacion/2026-11-01-tres-errores-de-mensajes-en-conversaciones-reales.md``,
error 1, «la correccion correcta»). A2 del carrito de ATT1 dice «Tu 10 % ya
vencio, pero si me respondes te lo reactivo», y hasta aca solo el boton exacto
«Quiero mi 10 %» lo cumplia (``cart_followups.is_cart_renewal_request``). En
ATT1 #471 y #517 la persona contesto escribiendo, el agente no sabia su cupon y
le dio el codigo general (o le dijo que «no vence»).

Con ``RECOVERY_COUPON_BY_REPLY_ENABLED``, en el turno del agente:

1. **La renovacion por respuesta.** Si lo que escribio la persona despues de
   nuestra ultima plantilla no es una baja (``opt_out``) ni un «no» claro
   (``lead_decline``), el bridge pide la misma renovacion que el boton
   (``request_recovery_renewal_by_reply_v1``, migracion 20261102000100): Z, la
   unica renovacion del caso, sale ya con el cupon 2. La base decide si hay un
   Z pendiente y si el mensaje es posterior a A2; sin Z no pasa nada. El
   agente contesta igual: a diferencia del boton, un texto puede traer una
   pregunta.
2. **El cupon en la entrada del agente** (``recovery_coupon``): el cupon
   personal vigente que la persona ya recibio (codigo, porcentaje, vencimiento
   en UTC y en la hora de su pais), o que la renovacion esta en camino. Si no
   hay ninguno, la entrada no lo trae y todo queda como antes.

Nada siembra un cupon nuevo: la regla de dos codigos por caso (regla 7 del
2026-10-17) no cambia. ``TIROIDES10`` sigue vivo (D6 de E-261): se retira
cuando el conocimiento de ATT1 use este dato.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from bridge.business_auto_reply import TEMPLATE_MARKERS
from bridge.cart_discount import country_for_phone, format_moment, timezone_for_country
from bridge.lead_decline import detect_declared_payment, detect_lead_decline
from bridge.opt_out import detect_explicit_opt_out

__all__ = [
    "RECOVERY_COUPON_CONTEXT_KEY",
    "prospect_texts_after_last_template",
    "recovery_coupon_context",
    "reply_may_renew",
]

RECOVERY_COUPON_CONTEXT_KEY = "recovery_coupon"

# message_type de la API de Chatwoot (enteros) y de los webhooks (texto).
_INCOMING = {0, "incoming"}
_OURS = {1, 3, "outgoing", "template"}


def _message_order(message: Mapping[str, object]) -> int:
    value = message.get("id")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _is_public(message: Mapping[str, object]) -> bool:
    return message.get("private") is not True


def _is_our_template(message: Mapping[str, object]) -> bool:
    message_type = message.get("message_type")
    if isinstance(message_type, bool) or message_type not in _OURS or not _is_public(message):
        return False
    attributes = message.get("content_attributes")
    return isinstance(attributes, Mapping) and any(
        marker in attributes for marker in TEMPLATE_MARKERS
    )


def _is_from_the_person(message: Mapping[str, object]) -> bool:
    message_type = message.get("message_type")
    if isinstance(message_type, bool) or message_type not in _INCOMING:
        return False
    if not _is_public(message):
        return False
    sender = message.get("sender")
    sender_type = sender.get("type") if isinstance(sender, Mapping) else None
    # Un entrante sin remitente es de la persona; uno de un usuario del equipo no.
    return sender_type in (None, "contact", "Contact")


def prospect_texts_after_last_template(history: Iterable[object]) -> list[object]:
    """Lo que escribio la persona despues de nuestra ultima plantilla, en orden.

    El historial crudo de Chatwoot (la API o el webhook). Sin ninguna plantilla
    nuestra en lo leido, todo lo que escribio: para no adelantar Z, el error
    barato es leer de mas.
    """
    messages = sorted(
        (message for message in history if isinstance(message, Mapping)),
        key=_message_order,
    )
    last_template = max(
        (index for index, message in enumerate(messages) if _is_our_template(message)),
        default=-1,
    )
    return [
        message.get("content")
        for message in messages[last_template + 1 :]
        if _is_from_the_person(message)
    ]


def reply_may_renew(history: Iterable[object]) -> bool:
    """Si lo que escribio la persona puede pedir la renovacion.

    No, si desde nuestra ultima plantilla hay una baja, un «no» claro o un pago
    declarado (E-486): Z es
    marketing, y a quien dijo que no, no se le manda (F-150). Cada mensaje se
    lee tambien solo (como si fuera la primera respuesta): «No, gracias» corta
    aunque venga despues de otro mensaje. Es mas estricto que el seguimiento a
    proposito: la renovacion la pide la primera respuesta a A2, y lo que viene
    despues ya da already_requested.
    """
    texts = prospect_texts_after_last_template(history)
    if detect_explicit_opt_out(texts) is not None:
        return False
    if detect_lead_decline(texts) is not None:
        return False
    # E-486: «ya pague» / «voy a pagar en OXXO» como respuesta a A2 no pide la
    # renovacion: Z es marketing con cupon para alguien que ya dijo que paga.
    if detect_declared_payment(texts) is not None:
        return False
    return all(detect_lead_decline([text]) is None for text in texts)


def _percent(discount: Decimal) -> int | str:
    value = (discount * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return int(value) if value == value.to_integral_value() else str(value)


def recovery_coupon_context(
    coupon: object,
    *,
    now: datetime,
    phone: str | None,
) -> dict[str, object] | None:
    """La entrada ``recovery_coupon`` del agente, o None si no hay que decir nada.

    ``coupon`` es lo que devuelve ``get_recovery_coupon_for_conversation``.
    El vencimiento va en UTC (como ``sent_at``) y en la hora del pais del
    numero, con el mismo formato que A1 y Z («mañana a las 9:05 a. m.»).
    """
    state = getattr(coupon, "state", None)
    if state == "renewal_in_progress":
        return {"state": "renewal_in_progress", "coupon_arrives_in_our_next_message": True}
    if state != "valid":
        return None
    code = getattr(coupon, "code", None)
    discount = getattr(coupon, "discount", None)
    expires_at = getattr(coupon, "expires_at", None)
    if (
        not isinstance(code, str)
        or not isinstance(discount, Decimal)
        or not isinstance(expires_at, datetime)
        or expires_at.tzinfo is None
        or now.tzinfo is None
        or expires_at <= now
    ):
        return None
    return {
        "state": "valid",
        "code": code,
        "discount_percent": _percent(discount),
        "expires_at": expires_at.astimezone(UTC).isoformat(),
        "expires_local": format_moment(
            expires_at, now=now, zone=timezone_for_country(country_for_phone(phone))
        ),
        "last_renewal": getattr(coupon, "ordinal", None) == 2,
    }
