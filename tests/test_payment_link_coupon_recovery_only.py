"""El cupon del 10 % va solo en la recuperacion (E-183, M8.c, PR B2).

Decision de Dan del 2026-10-29: «que el 10 % sea solamente para la
recuperacion». En Maribel, la conversacion 6 la abrio la persona por su cuenta,
ya decidida, y el link del agente salio con ``offDiscount=PROTOCOLO10%2B``. Con
``PAYMENT_LINK_COUPON_RECOVERY_ONLY=true`` el cupon fijo
(``PAYMENT_LINK_COUPON_CODE``) va solo si la conversacion tuvo un toque nuestro
de recuperacion, y el agente lo sabe por ``payment_link_action.coupon_applied``.

Historiales capturados (API de Chatwoot de produccion, anonimizados; el origen
de cada uno esta en su ``_fixture`` / ``_capture``):

* ``chatwoot_conversation_api_show_conv_158_20260924.json``: Johanna, inbox 9,
  el primer contacto del formulario (``recovery_first_touch_hash``) y la
  respuesta de la persona;
* ``chatwoot_daily_feedback_att1_conversations_20261015.json``: ATT1, inbox 11.
  La 120 trae la plantilla del carrito (``recovery_first_touch_hash``) y el
  seguimiento con cupon (``conversation_followup_command_key``); la 163 la abre
  la persona desde la landing y el modelo le manda el link: la misma forma que
  la conversacion 6 de Maribel.

``recovery_followup_hash`` no tiene captura propia: se deriva del primer
contacto capturado cambiando solo la clave, que es como la escribe
``ChatwootControlClient`` (``chatwoot.py``, el seguimiento del caso).
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest

from bridge.app import Settings, create_app
from bridge.payment_link_coupon import (
    RECOVERY_TOUCH_MARKERS,
    conversation_had_recovery_touch,
    mark_payment_link_coupon,
)
from test_payment_link_template_wiring import LEAD_JID, PREAMBLE, SECRET, _settings
from test_webhook import (
    StubChatwootClient,
    StubPaymentLinkSupabase,
    StubShadowProcessor,
    _post,
    _signed_headers,
)


FIXTURES = Path(__file__).parent / "fixtures"
CONV_158 = json.loads(
    (FIXTURES / "chatwoot_conversation_api_show_conv_158_20260924.json").read_text(
        encoding="utf-8"
    )
)["messages"]["payload"]
ATT1 = json.loads(
    (FIXTURES / "chatwoot_daily_feedback_att1_conversations_20261015.json").read_text(
        encoding="utf-8"
    )
)["conversations"]
CONV_120 = ATT1["120"]["messages"]
CONV_163 = ATT1["163"]["messages"]

COUPON = "PROTOCOLO10+"
CHECKOUT_URL = (
    "https://pay.hotmart.com/F106691755G?off=bxjge6zq"
    "&checkoutMode=10&src=hermes"
    "&sck=hermes%7Cv1%7C01K3F8QW7N2VYB4M6X9CDPTZRA"
)
WITH_COUPON = CHECKOUT_URL + "&offDiscount=PROTOCOLO10%2B"


def _first_touch() -> dict[str, object]:
    return copy.deepcopy(next(
        message for message in CONV_158
        if "recovery_first_touch_hash" in message["content_attributes"]
    ))


# --- Que es un toque de recuperacion -----------------------------------------


def test_the_form_first_touch_counts() -> None:
    assert conversation_had_recovery_touch(CONV_158) is True


def test_the_cart_template_and_the_coupon_followup_count() -> None:
    assert conversation_had_recovery_touch(CONV_120) is True
    followup_only = [
        message for message in CONV_120
        if "recovery_first_touch_hash" not in message["content_attributes"]
    ]
    # El seguimiento con cupon de las 24 h ya le ofrecio el 10 %.
    assert any(
        "conversation_followup_command_key" in message["content_attributes"]
        for message in followup_only
    )
    assert conversation_had_recovery_touch(followup_only) is True


def test_the_case_followup_counts() -> None:
    message = _first_touch()
    attributes = message["content_attributes"]
    attributes["recovery_followup_hash"] = attributes.pop("recovery_first_touch_hash")
    assert conversation_had_recovery_touch([message]) is True


def test_a_conversation_the_person_opened_does_not_count() -> None:
    # La 163: llega desde la landing y el modelo le manda el link.
    assert any(message["message_type"] == 1 for message in CONV_163)
    assert conversation_had_recovery_touch(CONV_163) is False


def test_a_marker_on_an_incoming_message_or_a_private_note_does_not_count() -> None:
    incoming = _first_touch()
    incoming["message_type"] = 0
    note = _first_touch()
    note["private"] = True
    as_text = _first_touch()
    as_text["message_type"] = "incoming"
    assert conversation_had_recovery_touch([incoming, note, as_text]) is False
    webhook_form = _first_touch()
    webhook_form["message_type"] = "outgoing"
    assert conversation_had_recovery_touch([webhook_form]) is True


def test_the_markers_are_the_ones_chatwoot_writes() -> None:
    source = (Path(__file__).parents[1] / "src" / "bridge" / "chatwoot.py").read_text(
        encoding="utf-8"
    )
    for marker in RECOVERY_TOUCH_MARKERS:
        assert f'"{marker}"' in source, marker


def test_bad_shapes_are_not_a_touch() -> None:
    assert conversation_had_recovery_touch(None) is False
    assert conversation_had_recovery_touch([None, "x", {"message_type": True}]) is False


# --- Lo que ve el agente -----------------------------------------------------


def _action() -> dict[str, object]:
    return {"payment_link_action": {"enabled": True}}


def test_with_the_flag_the_agent_is_told_whether_the_link_carries_it() -> None:
    organic, recovery, no_code = _action(), _action(), _action()
    mark_payment_link_coupon(organic, CONV_163, coupon_code=COUPON, recovery_only=True)
    mark_payment_link_coupon(recovery, CONV_120, coupon_code=COUPON, recovery_only=True)
    mark_payment_link_coupon(no_code, CONV_120, coupon_code=None, recovery_only=True)
    assert organic["payment_link_action"]["coupon_applied"] is False
    assert recovery["payment_link_action"]["coupon_applied"] is True
    assert no_code["payment_link_action"]["coupon_applied"] is False


def test_without_the_flag_or_without_the_link_the_context_does_not_change() -> None:
    off = _action()
    mark_payment_link_coupon(off, CONV_120, coupon_code=COUPON, recovery_only=False)
    assert off == _action()
    no_link: dict[str, object] = {}
    mark_payment_link_coupon(no_link, CONV_120, coupon_code=COUPON, recovery_only=True)
    assert no_link == {}


# --- La variable ---------------------------------------------------------------


_MIN_ENV = {
    "CHATWOOT_WEBHOOK_SECRET": "test-secret",
    "ALLOWED_WHATSAPP_JID": "12025550123@s.whatsapp.net",
    "CHATWOOT_AGENT_BOT_ID": "1",
    "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
    "CHATWOOT_ACCOUNT_ID": "1",
    "CHATWOOT_CONTROL_API_ACCESS_TOKEN": "test-control-token",
    "CHATWOOT_PAUSE_MACRO_ID": "1",
    "CHATWOOT_INBOX_ID": "9",
}


def test_the_flag_is_off_by_default_and_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in _MIN_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("PAYMENT_LINK_COUPON_RECOVERY_ONLY", raising=False)
    assert Settings.__dataclass_fields__["payment_link_coupon_recovery_only"].default is False
    assert Settings.from_env().payment_link_coupon_recovery_only is False
    monkeypatch.setenv("PAYMENT_LINK_COUPON_RECOVERY_ONLY", "yes")
    assert Settings.from_env().payment_link_coupon_recovery_only is False
    monkeypatch.setenv("PAYMENT_LINK_COUPON_RECOVERY_ONLY", " TRUE ")
    assert Settings.from_env().payment_link_coupon_recovery_only is True


# --- El recorrido entero: webhook firmado, worker, agente y link ------------------


def _history(prior: list[dict[str, object]]) -> list[dict[str, object]]:
    """El historial capturado, sin el link que ya mando el modelo, y el pedido."""

    kept = [
        copy.deepcopy(message) for message in prior
        if "appointment_setter_reply_hash" not in message["content_attributes"]
    ]
    last = max(int(message["created_at"]) for message in kept)
    return [*kept, {
        "id": 902,
        "created_at": last + 60,
        "message_type": 0,
        "private": False,
        "content": "Mandame el link de pago",
        "sender": {"type": "contact", "id": 20},
    }]


def _send_link(
    tmp_path: Path, prior: list[dict[str, object]], **overrides: object
) -> tuple[StubChatwootClient, StubShadowProcessor]:
    chatwoot = StubChatwootClient(messages=_history(prior))
    supabase = StubPaymentLinkSupabase()
    shadow = StubShadowProcessor({
        "decision": "send_payment_link",
        "qualification_status": "in_progress",
        "reason_code": "payment_link_requested",
        "reply": PREAMBLE,
        "captured_fields": {},
        "missing_fields": [],
    })
    app = create_app(
        _settings(tmp_path, **overrides),
        chatwoot_client=chatwoot,
        shadow_processor=shadow,
        supabase_client=supabase,  # type: ignore[arg-type]
    )
    payload: dict[str, object] = {
        "event": "message_created",
        "id": 902,
        "content": "Mandame el link de pago",
        "message_type": "incoming",
        "private": False,
        "account": {"id": 1},
        "inbox": {"id": 9},
        "conversation": {
            "id": 322,
            "inbox_id": 9,
            "contact_inbox": {"source_id": LEAD_JID},
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    response = _post(
        app, raw_body, _signed_headers(raw_body, secret=SECRET, delivery="cupon-e183")
    )
    asyncio.run(app.state.chatwoot_worker.run_once())
    assert response.status_code == 202
    assert len(supabase.finalize_calls) == 1
    assert supabase.finalize_calls[0]["status"] == "accepted_by_chatwoot"
    return chatwoot, shadow


def _sent_link(chatwoot: StubChatwootClient) -> str:
    assert len(chatwoot.reply_calls) == 1
    content = chatwoot.reply_calls[0]["content"]
    assert isinstance(content, str) and content.startswith(f"{PREAMBLE}\n")
    return content.removeprefix(f"{PREAMBLE}\n")


def _agent_action(shadow: StubShadowProcessor) -> dict[str, object]:
    assert len(shadow.calls) == 1
    action = shadow.calls[0][1]["payment_link_action"]
    assert isinstance(action, dict)
    return action


def test_who_came_on_their_own_gets_the_full_price_link(tmp_path: Path) -> None:
    chatwoot, shadow = _send_link(
        tmp_path, CONV_163,
        payment_link_coupon_code=COUPON, payment_link_coupon_recovery_only=True,
    )
    assert _sent_link(chatwoot) == CHECKOUT_URL
    assert _agent_action(shadow)["coupon_applied"] is False


@pytest.mark.parametrize("prior", [CONV_158, CONV_120], ids=["formulario", "carrito"])
def test_a_recovery_conversation_keeps_the_coupon(
    tmp_path: Path, prior: list[dict[str, object]]
) -> None:
    chatwoot, shadow = _send_link(
        tmp_path, prior,
        payment_link_coupon_code=COUPON, payment_link_coupon_recovery_only=True,
    )
    assert _sent_link(chatwoot) == WITH_COUPON
    assert _agent_action(shadow)["coupon_applied"] is True


def test_with_the_flag_off_everything_is_as_today(tmp_path: Path) -> None:
    chatwoot, shadow = _send_link(tmp_path, CONV_163, payment_link_coupon_code=COUPON)
    assert _sent_link(chatwoot) == WITH_COUPON
    assert "coupon_applied" not in _agent_action(shadow)


def test_with_the_flag_and_no_coupon_the_link_goes_as_always(tmp_path: Path) -> None:
    chatwoot, shadow = _send_link(
        tmp_path, CONV_120, payment_link_coupon_recovery_only=True
    )
    assert _sent_link(chatwoot) == CHECKOUT_URL
    assert _agent_action(shadow)["coupon_applied"] is False
