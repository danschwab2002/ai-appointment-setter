"""La bienvenida tras la compra no es un toque de recuperacion (E-204, hueco 18).

Con ``PAYMENT_LINK_COUPON_RECOVERY_ONLY=true`` el 10 % va en el link del agente
solo si la conversacion tuvo un toque nuestro de recuperacion, y
``conversation_had_recovery_touch`` lo reconoce por las marcas de
``content_attributes``. El primer toque de un flujo nativo sale por
``ChatwootClient.send_first_message``, que marca todo primer toque con
``recovery_first_touch_hash``: tambien la bienvenida de un flujo nativo con
``entra.permiso: compra``. Sin otra marca, quien compro, salio de la pausa y
le pidio el link al agente lo recibia con el cupon.

El arreglo: ese primer toque lleva ademas ``flow_engine_purchase_welcome:
true`` (``recovery_first_touch_hash`` se queda, la usan la validacion de
``send_first_message``, ``daily_feedback_export.py`` y
``business_auto_reply.py``) y un mensaje con esa marca no cuenta. El carrito
nativo (``formulario``) y el primer toque del legado siguen contando.

Datos: el mensaje es el body del POST que arma ``send_first_message`` en el
recorrido real del despachador (los dobles de
``test_flow_engine_native_dispatch.py``), puesto sobre la forma del primer
toque capturado de la conversacion 158 (como lo devuelve el GET de mensajes
de Chatwoot: ``message_type`` 1, ``sender`` agent_bot). El link se ve en el
recorrido entero del webhook de ``test_payment_link_coupon_recovery_only.py``.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_flow_engine_native_dispatch import (
    _REACTIVATION,
    _dispatcher,
    _NativeAuthority,
    _NativeChatwoot,
    _run,
    _step,
    _templates,
)
from test_payment_link_coupon_recovery_only import (
    CHECKOUT_URL,
    CONV_120,
    COUPON,
    WITH_COUPON,
    _first_touch,
    _send_link,
    _sent_link,
)

from bridge.chatwoot import FLOW_ENGINE_PURCHASE_WELCOME_MARKER, ChatwootClient
from bridge.payment_link_coupon import (
    conversation_had_recovery_touch,
    mark_payment_link_coupon,
)


def _sent_first_touch(authority: _NativeAuthority, chatwoot: _NativeChatwoot, tmp_path: Path) -> dict[str, Any]:
    _run(_dispatcher(authority, chatwoot, tmp_path))
    [message] = _templates(chatwoot)
    return message


def _as_chatwoot_returns_it(posted: dict[str, Any]) -> dict[str, Any]:
    """El POST del primer toque, con la forma del GET de mensajes de Chatwoot."""
    message = _first_touch()
    message["content"] = posted["content"]
    message["content_attributes"] = copy.deepcopy(posted["content_attributes"])
    return message


def _coupon_applied(history: list[dict[str, Any]]) -> object:
    context: dict[str, object] = {"payment_link_action": {"enabled": True}}
    mark_payment_link_coupon(context, history, coupon_code=COUPON, recovery_only=True)
    return context["payment_link_action"]["coupon_applied"]  # type: ignore[index]


def _welcome(tmp_path: Path) -> dict[str, Any]:
    return _sent_first_touch(_NativeAuthority(step=_step(entry_permission="compra")), _NativeChatwoot(), tmp_path)


def _native_cart(tmp_path: Path) -> dict[str, Any]:
    authority = _NativeAuthority(step=_step(
        entry_permission="formulario",
        template_name="att1_reactivacion_01",
        template_variables=("nombre",),
    ))
    chatwoot = _NativeChatwoot()
    chatwoot.catalog.append(copy.deepcopy(_REACTIVATION))
    return _sent_first_touch(authority, chatwoot, tmp_path)


def _legacy_cart(tmp_path: Path) -> dict[str, Any]:
    return _sent_first_touch(_NativeAuthority(native=False), _NativeChatwoot(), tmp_path)


# ------------------------------------------------------------ lo que sale


def test_only_the_purchase_welcome_carries_its_own_marker(tmp_path: Path) -> None:
    welcome = _welcome(tmp_path / "bienvenida")
    native_cart = _native_cart(tmp_path / "carrito-nativo")
    legacy_cart = _legacy_cart(tmp_path / "carrito-legado")

    assert welcome["template_params"]["category"] == "UTILITY"
    assert set(welcome["content_attributes"]) == {
        "recovery_first_touch_hash", FLOW_ENGINE_PURCHASE_WELCOME_MARKER,
    }
    assert welcome["content_attributes"][FLOW_ENGINE_PURCHASE_WELCOME_MARKER] is True
    assert native_cart["template_params"]["name"] == "att1_reactivacion_01"
    assert set(native_cart["content_attributes"]) == {"recovery_first_touch_hash"}
    assert set(legacy_cart["content_attributes"]) == {"recovery_first_touch_hash"}


def test_a_native_step_without_its_permission_is_not_marked(tmp_path: Path) -> None:
    # Una base sin 20261028000200: no se sabe si es la compra, queda como antes.
    message = _sent_first_touch(_NativeAuthority(step=_step(entry_permission=None)), _NativeChatwoot(), tmp_path)
    assert set(message["content_attributes"]) == {"recovery_first_touch_hash"}


# ----------------------------------------------------------- el cupon


def test_the_purchase_welcome_does_not_give_the_coupon(tmp_path: Path) -> None:
    history = [_as_chatwoot_returns_it(_welcome(tmp_path))]

    assert conversation_had_recovery_touch(history) is False
    assert _coupon_applied(history) is False


@pytest.mark.parametrize("sent", [_native_cart, _legacy_cart], ids=["carrito nativo", "carrito del legado"])
def test_a_cart_first_touch_still_gives_the_coupon(tmp_path: Path, sent: Any) -> None:
    history = [_as_chatwoot_returns_it(sent(tmp_path))]

    assert conversation_had_recovery_touch(history) is True
    assert _coupon_applied(history) is True


def test_a_welcome_next_to_a_recovery_touch_still_counts(tmp_path: Path) -> None:
    # La bienvenida no borra un toque de recuperacion que haya en la misma conversacion.
    history = [*copy.deepcopy(CONV_120), _as_chatwoot_returns_it(_welcome(tmp_path))]

    assert _coupon_applied(history) is True


def test_the_buyer_who_left_the_pause_gets_the_full_price_link(tmp_path: Path) -> None:
    # El recorrido entero del webhook: la bienvenida y despues el pedido del link.
    welcome = _as_chatwoot_returns_it(_welcome(tmp_path / "bienvenida"))

    chatwoot, _ = _send_link(
        tmp_path / "link", [welcome],
        payment_link_coupon_code=COUPON, payment_link_coupon_recovery_only=True,
    )

    assert _sent_link(chatwoot) == CHECKOUT_URL


def test_after_a_native_cart_the_link_keeps_the_coupon(tmp_path: Path) -> None:
    cart = _as_chatwoot_returns_it(_native_cart(tmp_path / "carrito"))

    chatwoot, _ = _send_link(
        tmp_path / "link", [cart],
        payment_link_coupon_code=COUPON, payment_link_coupon_recovery_only=True,
    )

    assert _sent_link(chatwoot) == WITH_COUPON


def test_a_marker_that_is_not_true_does_not_exempt_the_message() -> None:
    message = _first_touch()
    message["content_attributes"][FLOW_ENGINE_PURCHASE_WELCOME_MARKER] = "true"
    assert conversation_had_recovery_touch([message]) is True


# ------------------------------------------------ send_first_message, directo


def _client(posts: list[dict[str, Any]]) -> ChatwootClient:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        posts.append(body)
        return httpx.Response(200, json={
            "id": 501, "conversation_id": 200, "message_type": 1, "private": False,
            "content": body["content"], "content_attributes": body["content_attributes"],
        })

    return ChatwootClient(
        base_url="https://chatwoot.test", account_id=2, access_token="control-token",
        agent_bot_access_token="bot-token", agent_bot_id=99, transport=httpx.MockTransport(handler),
    )


@pytest.mark.parametrize("welcome", [True, False])
def test_send_first_message_adds_the_marker_only_for_the_welcome(welcome: bool) -> None:
    posts: list[dict[str, Any]] = []
    kwargs: dict[str, object] = {"purchase_welcome": True} if welcome else {}

    result = asyncio.run(_client(posts).send_first_message(
        conversation_id=200, content="Hola", delivery_id="attempt-1", **kwargs,
    ))

    assert result == {"status": "sent", "message_id": 501}
    [body] = posts
    expected = {"recovery_first_touch_hash"} | ({FLOW_ENGINE_PURCHASE_WELCOME_MARKER} if welcome else set())
    assert set(body["content_attributes"]) == expected


def test_the_marker_is_the_one_chatwoot_writes() -> None:
    source = (Path(__file__).parents[1] / "src" / "bridge" / "chatwoot.py").read_text(encoding="utf-8")
    assert FLOW_ENGINE_PURCHASE_WELCOME_MARKER == "flow_engine_purchase_welcome"
    assert f'"{FLOW_ENGINE_PURCHASE_WELCOME_MARKER}"' in source
