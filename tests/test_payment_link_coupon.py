"""El cupon fijo de la instancia en el link del agente (E-148, salida a de E-146).

Fixture: ``hotmart_checkout_coupon_maribel_20261027.json``, el checkout publico
de Hotmart medido el 2026-10-27 ~18:42Z y la forma del link que mando el agente
de Maribel en el E2E de ese dia (sck sintetico, sin telefonos). El cupon de la
instancia es ``PROTOCOLO10+``: aplica con el ``+`` codificado (``%2B``) y no con
el ``+`` literal, que llega como espacio.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bridge.app import Settings, create_app
from bridge.checkout_delivery import deliver_checkout_issuance_v2
from bridge.payment_link_coupon import (
    link_carries_coupon,
    payment_link_with_coupon,
    validate_payment_link_coupon_code,
)
from test_checkout_delivery import LINK_BODY, LINK_TEMPLATE, TemplateControl, _catalog
from test_payment_link_template_wiring import (
    LEAD_JID,
    PREAMBLE,
    _chatwoot,
    _deliver as _deliver_through_app,
    _settings,
)
from test_webhook import StubPaymentLinkSupabase


FIXTURE = json.loads(
    (
        Path(__file__).parent / "fixtures" / "hotmart_checkout_coupon_maribel_20261027.json"
    ).read_text(encoding="utf-8")
)
E2E_LINK = FIXTURE["link_del_agente_e2e"]
COUPON = FIXTURE["cupon_de_la_instancia"]
APPLIED_SUFFIXES = {row["sufijo"] for row in FIXTURE["medido"] if row["cupon_aplicado"]}
REJECTED_SUFFIXES = {
    row["sufijo"] for row in FIXTURE["medido"] if not row["cupon_aplicado"] and row["sufijo"]
}
E2E_LINK_WITH_COUPON = E2E_LINK + "&offDiscount=PROTOCOLO10%2B"
PREFIX = "https://pay.hotmart.com/"


# --- La funcion pura --------------------------------------------------------


def test_the_coupon_goes_encoded_as_hotmart_applies_it() -> None:
    url = payment_link_with_coupon(E2E_LINK, COUPON)

    assert url == E2E_LINK_WITH_COUPON
    added = url.removeprefix(E2E_LINK)
    # Lo que sumamos es exactamente lo que Hotmart aplico el 27/10.
    assert added in APPLIED_SUFFIXES
    # Y nunca la forma con el + literal, que dio hmc_024.
    assert added not in REJECTED_SUFFIXES


def test_without_the_variable_the_link_is_the_one_of_the_base() -> None:
    assert payment_link_with_coupon(E2E_LINK, None) == E2E_LINK


def test_a_personal_coupon_already_in_the_link_wins() -> None:
    personal = E2E_LINK + "&offDiscount=ATT1K7Q2M9XP"
    assert link_carries_coupon(personal) is True
    assert payment_link_with_coupon(personal, COUPON) == personal
    assert link_carries_coupon(E2E_LINK) is False


@pytest.mark.parametrize("code", ["PROTOCOLO10+", "DESC_10", "a-b", "X" * 64])
def test_valid_codes_pass(code: str) -> None:
    validate_payment_link_coupon_code(code)


@pytest.mark.parametrize(
    "code",
    ["PROTOCOLO 10", "DESC%2B", "a&b=c", "a/b", "ñandu", "", "X" * 65, "A#B"],
)
def test_anything_else_stops(code: str) -> None:
    with pytest.raises(ValueError, match="PAYMENT_LINK_COUPON_CODE"):
        validate_payment_link_coupon_code(code)


def test_a_link_that_is_not_hotmart_is_rejected() -> None:
    with pytest.raises(ValueError, match="payment_link_checkout_url_invalid"):
        payment_link_with_coupon("https://example.com/x?off=a", COUPON)


# --- La entrega: texto, boton y base coherentes -----------------------------


class MaribelSupabase:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def reserve_chatwoot_checkout_issuance_v2(self, **kwargs):
        self.calls.append(("reserve", kwargs))
        return SimpleNamespace(
            outcome="reserved",
            issuance_id="00000000-0000-0000-0000-000000000301",
            checkout_url_final=E2E_LINK,
        )

    async def authorize_chatwoot_checkout_issuance_v2(self, **kwargs):
        self.calls.append(("authorize", kwargs))
        return SimpleNamespace(
            outcome="request_started", issuance_id=kwargs["issuance_id"],
            status="request_started",
        )

    async def finalize_chatwoot_checkout_issuance_v2(self, **kwargs):
        self.calls.append(("finalize", kwargs))
        return SimpleNamespace(outcome="finalized", status=kwargs["status"])


class TextControl:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send_agent_bot_reply(self, **kwargs):
        self.sent.append(kwargs)
        assert await kwargs["pre_send_authorizer"]() is True
        return {"status": "sent", "message_id": 7001}


def _deliver(db, control, **extra):
    async def no_sleep(_seconds):
        return None

    return asyncio.run(deliver_checkout_issuance_v2(
        supabase=db,
        control_client=control,
        commercial_case_id="00000000-0000-0000-0000-000000000300",
        external_user_id="12025550123",
        chatwoot_account_id=3,
        chatwoot_inbox_id=13,
        chatwoot_conversation_id=9101,
        trigger_message_id=501,
        delivery_id="delivery-1",
        preamble="Perfecto. Acá tenés el link:",
        part_sleep=no_sleep,
        **extra,
    ))


def test_the_written_link_carries_the_coupon() -> None:
    db = MaribelSupabase()
    control = TextControl()

    result = _deliver(db, control, link_coupon_code=COUPON)

    assert result.outcome == "sent"
    assert control.sent[0]["content"] == f"Perfecto. Acá tenés el link:\n{E2E_LINK_WITH_COUPON}"
    # La base no cambia: reserva, autoriza y cierra por issuance_id, sin URL.
    assert [name for name, _ in db.calls] == ["reserve", "authorize", "finalize"]
    for _, kwargs in db.calls:
        assert not any("url" in key for key in kwargs)


def test_without_the_coupon_the_written_link_is_as_today() -> None:
    control = TextControl()
    _deliver(MaribelSupabase(), control)
    assert control.sent[0]["content"] == f"Perfecto. Acá tenés el link:\n{E2E_LINK}"


def test_the_button_and_the_written_copy_carry_the_same_link() -> None:
    control = TemplateControl(_catalog())
    result = _deliver(
        MaribelSupabase(), control,
        link_coupon_code=COUPON,
        link_template_name=LINK_TEMPLATE,
        link_template_language="es_EC",
    )

    assert result.outcome == "sent"
    sends = [kwargs for name, kwargs in control.calls if name == "send"]
    assert sends[0]["content"] == "Perfecto. Acá tenés el link:"
    assert sends[1]["content"] == f"{LINK_BODY}\n{E2E_LINK_WITH_COUPON}"
    button = sends[1]["template_params"]["processed_params"]["buttons"][0]["parameter"]
    assert button == E2E_LINK_WITH_COUPON.removeprefix(PREFIX)


# --- La app: la variable, el arranque y el recorrido entero -----------------


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


def test_the_variable_is_off_by_default_and_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in _MIN_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("PAYMENT_LINK_COUPON_CODE", raising=False)
    assert Settings.__dataclass_fields__["payment_link_coupon_code"].default is None
    assert Settings.from_env().payment_link_coupon_code is None
    monkeypatch.setenv("PAYMENT_LINK_COUPON_CODE", "   ")
    assert Settings.from_env().payment_link_coupon_code is None
    monkeypatch.setenv("PAYMENT_LINK_COUPON_CODE", " PROTOCOLO10+ ")
    assert Settings.from_env().payment_link_coupon_code == "PROTOCOLO10+"


def test_the_bridge_does_not_start_with_a_bad_code(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="PAYMENT_LINK_COUPON_CODE"):
        create_app(_settings(tmp_path, payment_link_coupon_code="PROTOCOLO 10"))


def test_the_agent_link_goes_out_with_the_coupon_through_the_app(tmp_path: Path) -> None:
    chatwoot = _chatwoot()
    supabase = StubPaymentLinkSupabase(reservation_url_override=E2E_LINK)

    _deliver_through_app(
        _settings(tmp_path, payment_link_coupon_code=COUPON), chatwoot, supabase
    )

    assert chatwoot.reply_calls == [{
        "conversation_id": 322,
        "trigger_message_id": 902,
        "delivery_id": "payment-link-template",
        "content": f"{PREAMBLE}\n{E2E_LINK_WITH_COUPON}",
        "expected_jid": LEAD_JID,
    }]
    assert len(supabase.finalize_calls) == 1
    assert supabase.finalize_calls[0]["status"] == "accepted_by_chatwoot"
