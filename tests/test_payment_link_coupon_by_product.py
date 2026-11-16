"""El cupon fijo de la instancia va solo en el link del principal (E-616).

Hallazgo de E-613 §1.5 sobre ``v1.40.0``: ``agent_link_coupon_code``
(``src/bridge/app.py``) comparaba el producto del link solo para el cupon de una
campana; el cupon fijo (``PAYMENT_LINK_COUPON_CODE``, ``PROTOCOLO10+`` en
Maribel, que es un cupon de Hotmart de El Protocolo) salia en el link de
cualquier producto. Con el combo ``forever-young`` declarado en
``[[productos]]``, el link del combo salia con ``offDiscount=PROTOCOLO10%2B`` y,
con ``PAYMENT_LINK_COUPON_RECOVERY_ONLY``, el turno le decia al agente
``coupon_applied: true`` antes de que el agente eligiera el producto.

Ahora: el cupon fijo va solo en el link del principal (o sin ``[[productos]]``),
y con varios productos ``coupon_applied`` no se adelanta (como el cupon de la
campana, D11 de E-338). Con un producto, todo igual: lo cubren
``test_payment_link_coupon_recovery_only.py`` y ``test_segment_link_coupon.py``.

Datos capturados (``tests/fixtures/``, origen en su ``_fixture``):

* el webhook ``message_created`` de la conversacion 200 del inbox 9
  (``chatwoot_message_created_audio_inbox_9_conv_200_20260928.json``, por
  ``_text_webhook``), con el texto del pedido del link;
* el primer contacto del formulario de la conversacion 158
  (``chatwoot_conversation_api_show_conv_158_20260924.json``, captura de
  produccion del 24/09, ``recovery_first_touch_hash``), como toque de
  recuperacion previo en el historial.

Los dos productos son los del manifiesto de prueba de ATT1 (``att1`` principal y
``att2``), de ``test_checkout_link_for_funnel._two_products_settings``.
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_agent_link_product import _LinkSupabase
from test_checkout_link_for_funnel import _two_products_settings
from test_whatsapp_inbound_equivalence import (
    PAYMENT_LINK_PROPOSAL,
    _process,
    _text_webhook,
)

from bridge.payment_link_coupon import fixed_coupon_fits_product

FIXTURES = Path(__file__).parent / "fixtures"
COUPON = "PROTOCOLO10+"
ENCODED = "&offDiscount=PROTOCOLO10%2B"
LINK_REPLY = str(PAYMENT_LINK_PROPOSAL["reply"])


def _first_touch_158() -> dict[str, Any]:
    payload = json.loads(
        (FIXTURES / "chatwoot_conversation_api_show_conv_158_20260924.json").read_text(
            encoding="utf-8"
        )
    )["messages"]["payload"]
    return copy.deepcopy(
        next(
            message
            for message in payload
            if "recovery_first_touch_hash" in message["content_attributes"]
        )
    )


def _link(
    tmp_path: Path,
    product: str,
    *,
    recovery_touch: bool = False,
    **overrides: Any,
) -> tuple[str, dict[str, Any]]:
    """La persona pide el link y el agente nombra ``product``: el link y la accion que vio."""

    webhook, history = _text_webhook("Envíame el enlace")
    if recovery_touch:
        touch = _first_touch_158()
        touch["created_at"] = int(history[0]["created_at"]) - 3600
        history = [touch, *history]
    settings = replace(
        _two_products_settings(tmp_path),
        payment_link_enabled=True,
        payment_link_coupon_code=COUPON,
        **overrides,
    )
    chatwoot, shadow, _ = _process(
        tmp_path,
        _LinkSupabase(),
        settings=settings,
        webhook=webhook,
        history=history,
        proposal={**PAYMENT_LINK_PROPOSAL, "product": product},
    )
    [reply] = chatwoot.reply_calls
    content = str(reply["content"])
    assert content.startswith(f"{LINK_REPLY}\n")
    [(_, context)] = shadow.calls
    action = context["payment_link_action"]
    assert isinstance(action, dict)
    return content.removeprefix(f"{LINK_REPLY}\n"), action


# ------------------------------------------------------------- la funcion pura


def test_the_fixed_coupon_fits_only_the_principal() -> None:
    assert fixed_coupon_fits_product(link_product="att1", principal_product="att1")
    assert not fixed_coupon_fits_product(link_product="att2", principal_product="att1")
    # Sin [[productos]] hay un solo producto: siempre va.
    assert fixed_coupon_fits_product(link_product=None, principal_product=None)


# --------------------------------------------------------------- por la app


def test_the_link_of_another_product_goes_without_the_fixed_coupon(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="bridge")

    url, _ = _link(tmp_path, "att2")

    assert "offDiscount" not in url
    assert "payment_link_coupon_other_product product=att2 conversation=200" in caplog.text


def test_the_link_of_the_principal_keeps_the_fixed_coupon(tmp_path: Path) -> None:
    url, action = _link(tmp_path, "att1")

    assert url.endswith(ENCODED)
    # Sin el flag de la recuperacion el agente no recibe nada, como siempre.
    assert "coupon_applied" not in action


def test_with_the_recovery_flag_the_link_of_another_product_goes_without_it(
    tmp_path: Path,
) -> None:
    """El caso de E-613: carrito con toque de recuperacion y el agente pide el link del combo."""

    url, action = _link(
        tmp_path, "att2", recovery_touch=True, payment_link_coupon_recovery_only=True
    )

    assert "offDiscount" not in url
    assert action["coupon_applied"] is False


def test_with_two_products_the_agent_is_not_told_before_it_names_the_product(
    tmp_path: Path,
) -> None:
    """Con varios productos ``coupon_applied`` no se adelanta, y el link sale como se le dijo."""

    url, action = _link(
        tmp_path, "att1", recovery_touch=True, payment_link_coupon_recovery_only=True
    )

    assert action["coupon_applied"] is False
    assert "offDiscount" not in url
