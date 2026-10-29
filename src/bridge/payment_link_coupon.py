"""El cupon fijo de la instancia en el link que manda el agente.

Decision de Dan del 2026-10-27 (E-146, salida a): una instancia cuyo carrito ya
da un cupon (Maribel: ``PROTOCOLO10+``, el mismo que manda el bot de GHL) puede
pedir que el link de ``send_payment_link`` llegue con ese cupon ya cargado. La
variable ``PAYMENT_LINK_COUPON_CODE`` arranca vacia: vacia, el link sale como
siempre. ATT1 y Johanna no la cargan.

Medido el 2026-10-27 ~18:42Z en el checkout publico de Hotmart
(``F107441770M?off=u8ikyx78&checkoutMode=10``, el ``__NUXT_DATA__`` del HTML):

* ``&offDiscount=PROTOCOLO10%2B`` aplica el cupon (``isCouponApplied: true``,
  1.204,31 -> 1.083,88 MXN), tambien con ``src`` y ``sck`` adelante;
* ``&offDiscount=PROTOCOLO10+`` (el ``+`` literal) llega como espacio y da el
  error ``hotmart_checkout_error_hmc_024``.

Por eso el codigo admite ``+`` y se codifica con ``quote(..., safe="")``.

Como el seguimiento con cupon (``followup_button_suffix``), el link que emitio
la base (``checkout_url_final``) no se toca: el cupon se suma al final en el
bridge, la fila de la base guarda el link sin cupon y la atribucion por ``sck``
no cambia. Sin migracion.

Si el link de la base ya trae un ``offDiscount`` (un cupon personal del caso,
F-046), manda ese y el fijo no se suma. Medido sobre ``origin/main`` 3484963:
la reserva del link del agente no arma hoy ningun ``offDiscount``, asi que la
regla es la convivencia para cuando llegue el personal.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, quote, urlsplit


HOTMART_CHECKOUT_PREFIX = "https://pay.hotmart.com/"
# Lo mismo que acepta el seguimiento (``COUPON_CODE_RE``) mas el ``+`` de
# ``PROTOCOLO10+``. ``COUPON_CODE_RE`` no se toca: la comparten el seguimiento
# y el esquema de flujos.
PAYMENT_LINK_COUPON_CODE_RE = re.compile(r"[A-Za-z0-9_+-]{1,64}")
OFF_DISCOUNT_PARAM = "offDiscount"


def validate_payment_link_coupon_code(code: str | None) -> None:
    """Frena al arrancar si ``PAYMENT_LINK_COUPON_CODE`` tiene otra cosa."""

    if code is None:
        return
    if not isinstance(code, str) or not PAYMENT_LINK_COUPON_CODE_RE.fullmatch(code):
        raise ValueError(
            "PAYMENT_LINK_COUPON_CODE must match [A-Za-z0-9_+-]{1,64}"
        )


def link_carries_coupon(checkout_url: str) -> bool:
    """Si el link ya trae un ``offDiscount`` (por ejemplo, un cupon personal)."""

    query = urlsplit(checkout_url).query
    return any(
        key == OFF_DISCOUNT_PARAM
        for key, _ in parse_qsl(query, keep_blank_values=True)
    )


def payment_link_with_coupon(checkout_url_final: str, coupon_code: str | None) -> str:
    """El link que ve la persona: el de la base mas el cupon fijo, codificado.

    Sin cupon, el link de la base tal cual. Con un ``offDiscount`` ya en el
    link, tambien tal cual: el cupon personal manda sobre el fijo.
    """

    if (
        not isinstance(checkout_url_final, str)
        or not checkout_url_final.startswith(HOTMART_CHECKOUT_PREFIX)
        or "?" not in checkout_url_final
        or "#" in checkout_url_final
    ):
        raise ValueError("payment_link_checkout_url_invalid")
    if coupon_code is None:
        return checkout_url_final
    validate_payment_link_coupon_code(coupon_code)
    if link_carries_coupon(checkout_url_final):
        return checkout_url_final
    return (
        checkout_url_final
        + "&"
        + OFF_DISCOUNT_PARAM
        + "="
        + quote(coupon_code, safe="")
    )


# --- Solo en la recuperacion (E-183, M8.c, decision de Dan del 2026-10-29) ---
#
# «Que el 10 % sea solamente para la recuperacion»: con
# ``PAYMENT_LINK_COUPON_RECOVERY_ONLY=true``, el cupon fijo va en el link del
# agente solo si la conversacion tuvo un toque nuestro de recuperacion. A quien
# llega por su cuenta con la compra decidida (Maribel, conversacion 6 del
# 29/10), el link sale a precio completo. Con el flag apagado, todo como antes.
#
# Un toque de recuperacion es un mensaje nuestro (saliente, no una nota
# privada) con alguna de estas marcas en ``content_attributes``, que escribe
# ``chatwoot.py`` y lee igual ``daily_feedback_export.py``:
#
# * ``recovery_first_touch_hash``: el primer contacto (formulario, carrito,
#   pago rechazado);
# * ``recovery_followup_hash``: el seguimiento del caso;
# * ``conversation_followup_command_key``: el seguimiento con cupon de las
#   24 h, que ya le ofrecio el cupon a la persona.
#
# Sin limite de tiempo (D1 de E-178). Las marcas en un entrante no cuentan.
RECOVERY_TOUCH_MARKERS = (
    "recovery_first_touch_hash",
    "recovery_followup_hash",
    "conversation_followup_command_key",
)
# 1 = outgoing y 3 = template en la API de Chatwoot; los webhooks lo mandan
# como texto.
_OUR_MESSAGE_TYPES = {1, 3, "outgoing", "template"}


def conversation_had_recovery_touch(history: object) -> bool:
    """Si en el historial crudo de Chatwoot hay un toque nuestro de recuperacion."""

    if not isinstance(history, list):
        return False
    for message in history:
        if not isinstance(message, dict):
            continue
        message_type = message.get("message_type")
        if isinstance(message_type, bool) or message_type not in _OUR_MESSAGE_TYPES:
            continue
        if message.get("private") is True:
            continue
        attributes = message.get("content_attributes")
        if isinstance(attributes, dict) and any(
            marker in attributes for marker in RECOVERY_TOUCH_MARKERS
        ):
            return True
    return False


def mark_payment_link_coupon(
    context: dict[str, object],
    history: object,
    *,
    coupon_code: str | None,
    recovery_only: bool,
) -> None:
    """Le dice al agente si el link que va a salir trae el cupon.

    Solo con el flag y con el link del agente prendido
    (``payment_link_action`` en el contexto): ``coupon_applied`` es true o
    false. Con el flag apagado el contexto no cambia.
    """

    if not recovery_only:
        return
    action = context.get("payment_link_action")
    if not isinstance(action, dict):
        return
    action["coupon_applied"] = coupon_code is not None and (
        conversation_had_recovery_touch(history)
    )


def coupon_applied_in_context(context: object) -> bool | None:
    """Lo que ya se decidio en el turno, o None si no se decidio."""

    if not isinstance(context, dict):
        return None
    action = context.get("payment_link_action")
    if not isinstance(action, dict):
        return None
    applied = action.get("coupon_applied")
    return applied if isinstance(applied, bool) else None
