"""El cableado del link de pago en el boton de una plantilla.

Recorre la app entera con los stubs de ``test_webhook.py`` y el catalogo de
plantillas capturado del inbox 9 (``chatwoot_message_templates_inbox_9_20261001.json``),
con ``johanna_enlace_pago_01`` tal como la aprobo Meta.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from bridge.app import Settings, create_app
from test_payment_link_template import (
    LINK_BODY,
    LINK_TEMPLATE,
    catalog_with_link_template,
)
from test_webhook import (
    StubChatwootClient,
    StubPaymentLinkSupabase,
    StubShadowProcessor,
    _post,
    _signed_headers,
)


SECRET = "webhook-secret"
PREAMBLE = "Sí, claro. Podés completar tu compra acá:"
CHECKOUT_URL = (
    "https://pay.hotmart.com/F106691755G?off=bxjge6zq"
    "&checkoutMode=10&src=hermes"
    "&sck=hermes%7Cv1%7C01K3F8QW7N2VYB4M6X9CDPTZRA"
)
LEAD_JID = "12025550124@s.whatsapp.net"


class CatalogChatwoot(StubChatwootClient):
    """El stub de la app, con el catalogo del inbox y los template_params."""

    def __init__(self, *, catalog: dict[str, object], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.catalog = catalog
        self.inbox_reads: list[int] = []
        self.sent_template_params: list[dict[str, object] | None] = []

    async def get_inbox(self, *, inbox_id: int) -> dict[str, object]:
        self.inbox_reads.append(inbox_id)
        return self.catalog

    async def send_agent_bot_reply(
        self,
        *,
        template_params: dict[str, object] | None = None,
        **kwargs: Any,
    ) -> dict[str, object]:
        self.sent_template_params.append(template_params)
        result = await super().send_agent_bot_reply(**kwargs)
        # La plantilla es otro mensaje de Chatwoot: otro id.
        if template_params is not None and result.get("status") == "sent":
            return {**result, "message_id": 901}
        return result


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "webhook_secret": SECRET,
        "allowed_jid": "12025550123@s.whatsapp.net",
        "capture_dir": tmp_path,
        "max_age_seconds": 300,
        "agent_bot_id": 1,
        "chatwoot_account_id": 1,
        "chatwoot_inbox_id": 9,
        "chatwoot_cut_b_admission_enabled": True,
        "chatwoot_cut_b_scope_key": "libre-de-ansiedad-inbound",
        "chatwoot_cut_b_scope_version": 2,
        "chatwoot_cut_b_agent_enabled": True,
        "chatwoot_scoped_inbound_senders_enabled": True,
        "automated_replies_enabled": True,
        "chatwoot_durable_opt_out_enabled": True,
        "chatwoot_human_pause_enabled": True,
        "chatwoot_opt_out_macro_id": 2,
        "opt_out_projection_worker_id": "opt-out-test",
        "human_handoff_admission_enabled": True,
        "human_handoff_projection_enabled": True,
        "handoff_projection_policy_key": "lancemos-inbound-handoff",
        "handoff_projection_policy_version": 1,
        "human_handoff_projection_worker_id": "handoff-projection-test",
        "payment_link_enabled": True,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _chatwoot() -> CatalogChatwoot:
    return CatalogChatwoot(
        catalog=catalog_with_link_template(),
        messages=[{
            "id": 902,
            "created_at": 1789164000,
            "message_type": 0,
            "private": False,
            "content": "Mandame el link de pago",
            "sender": {"type": "contact", "id": 20},
        }],
    )


def _deliver(
    settings: Settings,
    chatwoot: CatalogChatwoot,
    supabase: StubPaymentLinkSupabase,
) -> list[float]:
    """El lead pide el link y el agente decide send_payment_link."""
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
    shadow = StubShadowProcessor({
        "decision": "send_payment_link",
        "qualification_status": "in_progress",
        "reason_code": "payment_link_requested",
        "reply": PREAMBLE,
        "captured_fields": {},
        "missing_fields": [],
    })
    delays: list[float] = []

    async def record_delay(seconds: float) -> None:
        delays.append(seconds)

    app = create_app(
        settings,
        chatwoot_client=chatwoot,
        shadow_processor=shadow,
        supabase_client=supabase,  # type: ignore[arg-type]
        reply_part_sleep=record_delay,
    )
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=SECRET, delivery="payment-link-template"),
    )
    asyncio.run(app.state.chatwoot_worker.run_once())
    assert response.status_code == 202
    return delays


def _written_link_call() -> dict[str, object]:
    return {
        "conversation_id": 322,
        "trigger_message_id": 902,
        "delivery_id": "payment-link-template",
        "content": f"{PREAMBLE}\n{CHECKOUT_URL}",
        "expected_jid": LEAD_JID,
    }


def test_configured_template_sends_the_link_in_its_button(tmp_path: Path) -> None:
    chatwoot = _chatwoot()
    supabase = StubPaymentLinkSupabase()

    delays = _deliver(
        _settings(
            tmp_path,
            payment_link_template_name=LINK_TEMPLATE,
            payment_link_template_language="es_EC",
            reply_part_delay_seconds=2.5,
        ),
        chatwoot,
        supabase,
    )

    assert chatwoot.inbox_reads == [9]
    assert chatwoot.reply_calls == [
        {
            "conversation_id": 322,
            "trigger_message_id": 902,
            "delivery_id": "payment-link-template",
            "content": PREAMBLE,
            "part_index": 1,
            "part_count": 2,
            "prior_parts": (),
            "expected_jid": LEAD_JID,
        },
        {
            "conversation_id": 322,
            "trigger_message_id": 902,
            "delivery_id": "payment-link-template",
            "content": f"{LINK_BODY}\n{CHECKOUT_URL}",
            "part_index": 2,
            "part_count": 2,
            "prior_parts": (PREAMBLE,),
            "expected_jid": LEAD_JID,
        },
    ]
    assert chatwoot.sent_template_params == [
        None,
        {
            "name": LINK_TEMPLATE,
            "category": "MARKETING",
            "language": "es_EC",
            "processed_params": {
                "buttons": [{
                    "type": "url",
                    "parameter": CHECKOUT_URL.removeprefix("https://pay.hotmart.com/"),
                }],
            },
        },
    ]
    # La pausa entre partes es la del splitter, leida de la configuracion.
    assert delays == [2.5]
    # La emision se autoriza una sola vez, justo antes de la plantilla, y se
    # cierra con el mensaje de la plantilla.
    assert chatwoot.pre_send_authorization_calls == 1
    assert len(supabase.prepare_calls) == 1
    assert supabase.finalize_calls == [{
        "issuance_id": "00000000-0000-0000-0000-000000000204",
        "status": "accepted_by_chatwoot",
        "chatwoot_message_id": 901,
        "failure_code": None,
        "now": supabase.finalize_calls[0]["now"],
    }]


def test_without_template_the_link_is_written_as_before(tmp_path: Path) -> None:
    chatwoot = _chatwoot()
    supabase = StubPaymentLinkSupabase()

    delays = _deliver(_settings(tmp_path), chatwoot, supabase)

    # Ni siquiera se lee el catalogo.
    assert chatwoot.inbox_reads == []
    assert chatwoot.reply_calls == [_written_link_call()]
    assert chatwoot.sent_template_params == [None]
    assert delays == []
    assert supabase.finalize_calls[0]["chatwoot_message_id"] == 900


def test_template_missing_from_the_catalog_writes_the_link(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    chatwoot = _chatwoot()
    supabase = StubPaymentLinkSupabase()

    with caplog.at_level(logging.WARNING, logger="bridge.checkout_delivery"):
        delays = _deliver(
            _settings(
                tmp_path,
                payment_link_template_name="johanna_enlace_pago_02",
                payment_link_template_language="es_EC",
            ),
            chatwoot,
            supabase,
        )

    assert chatwoot.inbox_reads == [9]
    assert chatwoot.reply_calls == [_written_link_call()]
    assert chatwoot.sent_template_params == [None]
    assert delays == []
    assert supabase.finalize_calls[0]["status"] == "accepted_by_chatwoot"
    assert (
        "payment_link_template_unavailable reason=payment_link_template_not_found"
        in caplog.text
    )
    assert "pay.hotmart.com" not in caplog.text


def _settings_from_env(monkeypatch: pytest.MonkeyPatch) -> Settings:
    for name, value in {
        "CHATWOOT_WEBHOOK_SECRET": "unused",
        "CHATWOOT_AGENT_BOT_ID": "1",
        "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
        "CHATWOOT_ACCOUNT_ID": "1",
        "CHATWOOT_CONTROL_API_ACCESS_TOKEN": "unused",
        "CHATWOOT_PAUSE_MACRO_ID": "1",
        "CHATWOOT_INBOX_ID": "9",
    }.items():
        monkeypatch.setenv(name, value)
    return Settings.from_env()


def test_payment_link_template_defaults_to_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PAYMENT_LINK_TEMPLATE_NAME", raising=False)
    monkeypatch.delenv("PAYMENT_LINK_TEMPLATE_LANGUAGE", raising=False)
    settings = _settings_from_env(monkeypatch)
    assert settings.payment_link_template_name is None
    assert settings.payment_link_template_language is None


def test_blank_payment_link_template_stays_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAYMENT_LINK_TEMPLATE_NAME", "   ")
    monkeypatch.setenv("PAYMENT_LINK_TEMPLATE_LANGUAGE", "")
    settings = _settings_from_env(monkeypatch)
    assert settings.payment_link_template_name is None
    assert settings.payment_link_template_language is None


def test_payment_link_template_is_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PAYMENT_LINK_TEMPLATE_NAME", " johanna_enlace_pago_01 ")
    monkeypatch.setenv("PAYMENT_LINK_TEMPLATE_LANGUAGE", "es_EC")
    settings = _settings_from_env(monkeypatch)
    assert settings.payment_link_template_name == "johanna_enlace_pago_01"
    assert settings.payment_link_template_language == "es_EC"
