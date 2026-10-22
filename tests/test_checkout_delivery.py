import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from bridge.chatwoot import ChatwootClient
from bridge.checkout_delivery import deliver_checkout_issuance_v2


URL = "https://pay.hotmart.com/F106691755G?off=bxjge6zq&checkoutMode=10&src=hermes&sck=hermes%7Cv1%7C01K5ABCDEFX2VYB4M6X9CDPTZR"


class FakeSupabase:
    def __init__(self, reserve_outcome="reserved", authorize_outcome="request_started"):
        self.reserve_outcome = reserve_outcome
        self.authorize_outcome = authorize_outcome
        self.calls = []

    async def reserve_chatwoot_checkout_issuance_v2(self, **kwargs):
        self.calls.append(("reserve", kwargs))
        full = self.reserve_outcome in {"reserved", "request_started_replay", "already_accepted", "delivery_unknown"}
        return SimpleNamespace(
            outcome=self.reserve_outcome,
            issuance_id="00000000-0000-0000-0000-000000000301" if full else None,
            checkout_url_final=URL if full else None,
        )

    async def authorize_chatwoot_checkout_issuance_v2(self, **kwargs):
        self.calls.append(("authorize", kwargs))
        return SimpleNamespace(
            outcome=self.authorize_outcome,
            issuance_id=kwargs["issuance_id"],
            status={
                "request_started": "request_started",
                "request_started_replay": "request_started",
                "already_accepted": "accepted_by_chatwoot",
                "delivery_unknown": "delivery_unknown",
            }.get(self.authorize_outcome, "reserved"),
        )

    async def finalize_chatwoot_checkout_issuance_v2(self, **kwargs):
        self.calls.append(("finalize", kwargs))
        return SimpleNamespace(outcome="finalized", status=kwargs["status"])


class SendingControl:
    async def send_agent_bot_reply(self, **kwargs):
        assert await kwargs["pre_send_authorizer"]() is True
        return {"status": "sent", "message_id": 7001}


class DuplicateControl:
    async def send_agent_bot_reply(self, **kwargs):
        return {"status": "duplicate", "message_id": 7002}


class BlockingControl:
    called = False

    async def send_agent_bot_reply(self, **kwargs):
        self.called = True
        return {"status": "blocked", "reason": "human_assignee_present"}


def _deliver(db, control):
    return asyncio.run(deliver_checkout_issuance_v2(
        supabase=db,
        control_client=control,
        commercial_case_id="00000000-0000-0000-0000-000000000300",
        external_user_id="12025550123",
        chatwoot_account_id=1,
        chatwoot_inbox_id=9,
        chatwoot_conversation_id=9101,
        trigger_message_id=501,
        delivery_id="delivery-1",
        preamble="Perfecto. Acá tenés el link:",
        expected_jid="12025550123@s.whatsapp.net",
    ))


def test_first_send_reserves_authorizes_and_finalizes() -> None:
    db = FakeSupabase()
    result = _deliver(db, SendingControl())
    assert result.outcome == "sent"
    assert [name for name, _ in db.calls] == ["reserve", "authorize", "finalize"]
    assert db.calls[-1][1]["status"] == "accepted_by_chatwoot"


def test_delivery_unknown_reconciles_only_from_exact_duplicate() -> None:
    db = FakeSupabase(reserve_outcome="delivery_unknown", authorize_outcome="delivery_unknown")
    result = _deliver(db, DuplicateControl())
    assert result.outcome == "reconciled"
    assert [name for name, _ in db.calls] == ["reserve", "authorize", "finalize"]
    assert db.calls[-1][1]["chatwoot_message_id"] == 7002


def test_live_chatwoot_block_leaves_reserved_row_without_post_authorization() -> None:
    db = FakeSupabase()
    control = BlockingControl()
    result = _deliver(db, control)
    assert result.outcome == "blocked"
    assert result.reason == "human_assignee_present"
    assert [name for name, _ in db.calls] == ["reserve"]


def test_durable_reservation_block_never_calls_chatwoot() -> None:
    db = FakeSupabase(reserve_outcome="blocked_opt_out")
    control = BlockingControl()
    result = _deliver(db, control)
    assert result.outcome == "blocked"
    assert result.reason == "blocked_opt_out"
    assert control.called is False


def test_live_inbox_mismatch_blocks_before_post_authorization(tmp_path: Path) -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/conversations/9101"):
            return httpx.Response(200, json={
                "id": 9101,
                "inbox_id": 10,
                "status": "open",
                "meta": {
                    "assignee": None,
                    "sender": {"identifier": "12025550123@s.whatsapp.net"},
                },
            })
        pytest.fail("An inbox mismatch must stop before any further Chatwoot request")

    db = FakeSupabase()
    client = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=1,
        access_token="control-token",
        allowed_jid="12025550123@s.whatsapp.net",
        agent_bot_access_token="agent-bot-token",
        agent_bot_id=1,
        reply_dir=tmp_path,
        transport=httpx.MockTransport(handler),
    )

    result = _deliver(db, client)

    assert result.outcome == "blocked"
    assert result.reason == "conversation_scope_changed"
    assert [name for name, _ in db.calls] == ["reserve"]
    assert len(requests) == 1


# --- El link en el boton de una plantilla -------------------------------------
#
# El catalogo es el del inbox 9 capturado el 2026-10-01 a las 19:58Z, con la
# plantilla del link tal como la aprobo Meta: se cargo como UTILITY y quedo
# MARKETING.

CATALOG_FIXTURE = (
    Path(__file__).parent / "fixtures" / "chatwoot_message_templates_inbox_9_20261001.json"
)
LINK_TEMPLATE = "johanna_enlace_pago_01"
LINK_BODY = (
    "Aquí tienes tu enlace de pago. Toca el botón de abajo para ir directo al "
    "pago seguro en Hotmart!\nSi tienes alguna duda antes de pagar, escríbeme "
    "por aquí."
)
PREAMBLE = "Perfecto. Acá tenés el link:"


def _catalog(*, with_link_template=True, **changes):
    catalog = json.loads(CATALOG_FIXTURE.read_text(encoding="utf-8"))
    templates = catalog["message_templates"]
    link = next(template for template in templates if template["name"] == LINK_TEMPLATE)
    if not with_link_template:
        # Como antes de que Meta la aprobara: la del link no esta en el catalogo.
        templates.remove(link)
    link.update(changes)
    return catalog


class TemplateControl:
    """Chatwoot de prueba: devuelve el catalogo y registra cada parte enviada."""

    def __init__(self, catalog, *, first=None, second=None, inbox_error=None):
        self.catalog = catalog
        self.first = first or {"status": "sent", "message_id": 7101}
        self.second = second or {"status": "sent", "message_id": 7102}
        self.inbox_error = inbox_error
        self.calls = []

    async def get_inbox(self, *, inbox_id):
        self.calls.append(("get_inbox", {"inbox_id": inbox_id}))
        if self.inbox_error is not None:
            raise self.inbox_error
        return self.catalog

    async def send_agent_bot_reply(self, **kwargs):
        self.calls.append(("send", kwargs))
        if kwargs.get("part_count") == 2 and kwargs.get("part_index") == 1:
            return self.first
        if self.second.get("status") == "sent":
            assert await kwargs["pre_send_authorizer"]() is True
        return self.second


def _deliver_with_template(db, control, sleeps):
    async def fake_sleep(seconds):
        sleeps.append(seconds)

    return asyncio.run(deliver_checkout_issuance_v2(
        supabase=db,
        control_client=control,
        commercial_case_id="00000000-0000-0000-0000-000000000300",
        external_user_id="12025550123",
        chatwoot_account_id=1,
        chatwoot_inbox_id=9,
        chatwoot_conversation_id=9101,
        trigger_message_id=501,
        delivery_id="delivery-1",
        preamble=PREAMBLE,
        expected_jid="12025550123@s.whatsapp.net",
        link_template_name=LINK_TEMPLATE,
        link_template_language="es_EC",
        part_delay_seconds=2.5,
        part_sleep=fake_sleep,
    ))


def _sends(control):
    return [kwargs for name, kwargs in control.calls if name == "send"]


def test_the_link_goes_in_the_template_button_after_the_agent_text() -> None:
    db = FakeSupabase()
    control = TemplateControl(_catalog())
    sleeps = []

    result = _deliver_with_template(db, control, sleeps)

    assert result.outcome == "sent"
    assert result.chatwoot_message_id == 7102
    assert control.calls[0] == ("get_inbox", {"inbox_id": 9})
    first, second = _sends(control)
    # El texto del agente sale solo, sin link y sin autorizar la emision.
    assert first["content"] == PREAMBLE
    assert (first["part_index"], first["part_count"], first["prior_parts"]) == (1, 2, ())
    assert "pre_send_authorizer" not in first
    assert "template_params" not in first
    assert sleeps == [2.5]
    # El link va en el boton, entero, y es la parte que autoriza la emision.
    assert second["content"] == f"{LINK_BODY}\n{URL}"
    assert (second["part_index"], second["part_count"]) == (2, 2)
    assert second["prior_parts"] == (PREAMBLE,)
    assert second["template_params"] == {
        "name": LINK_TEMPLATE,
        "category": "MARKETING",
        "language": "es_EC",
        "processed_params": {
            "buttons": [{"type": "url", "parameter": URL.removeprefix("https://pay.hotmart.com/")}],
        },
    }
    assert second["expected_jid"] == "12025550123@s.whatsapp.net"
    assert [name for name, _ in db.calls] == ["reserve", "authorize", "finalize"]
    assert db.calls[-1][1]["status"] == "accepted_by_chatwoot"
    assert db.calls[-1][1]["chatwoot_message_id"] == 7102


@pytest.mark.parametrize(
    ("catalog", "inbox_error", "reason"),
    [
        (_catalog(with_link_template=False), None, "payment_link_template_not_found"),
        (_catalog(status="PAUSED"), None, "payment_link_template_not_approved"),
        (_catalog(), httpx.ConnectError("boom"), "ConnectError"),
    ],
)
def test_without_a_usable_template_the_link_goes_as_text(
    catalog, inbox_error, reason, caplog
) -> None:
    db = FakeSupabase()
    control = TemplateControl(catalog, inbox_error=inbox_error)
    sleeps = []

    with caplog.at_level(logging.WARNING, logger="bridge.checkout_delivery"):
        result = _deliver_with_template(db, control, sleeps)

    assert result.outcome == "sent"
    (only,) = _sends(control)
    assert only["content"] == f"{PREAMBLE}\n{URL}"
    assert "part_count" not in only
    assert "template_params" not in only
    assert sleeps == []
    assert [name for name, _ in db.calls] == ["reserve", "authorize", "finalize"]
    assert f"payment_link_template_unavailable reason={reason}" in caplog.text


def test_a_blocked_agent_text_never_authorizes_the_link() -> None:
    db = FakeSupabase()
    control = TemplateControl(
        _catalog(), first={"status": "blocked", "reason": "human_assignee_present"}
    )
    sleeps = []

    result = _deliver_with_template(db, control, sleeps)

    assert result.outcome == "blocked"
    assert result.reason == "human_assignee_present"
    assert len(_sends(control)) == 1
    assert sleeps == []
    assert [name for name, _ in db.calls] == ["reserve"]


def test_a_retry_after_the_text_went_out_sends_only_the_template() -> None:
    # El intento anterior mando el texto y se corto antes de autorizar el link:
    # la emision sigue reservada y la plantilla sale ahora.
    db = FakeSupabase()
    control = TemplateControl(
        _catalog(), first={"status": "duplicate", "message_id": 7101}
    )
    sleeps = []

    result = _deliver_with_template(db, control, sleeps)

    assert result.outcome == "sent"
    assert result.chatwoot_message_id == 7102
    # El texto ya estaba en Chatwoot: no se espera de nuevo.
    assert sleeps == []
    assert [name for name, _ in db.calls] == ["reserve", "authorize", "finalize"]


def test_a_retry_after_both_parts_reconciles_from_the_template_message() -> None:
    db = FakeSupabase(
        reserve_outcome="delivery_unknown", authorize_outcome="delivery_unknown"
    )
    control = TemplateControl(
        _catalog(),
        first={"status": "duplicate", "message_id": 7101},
        second={"status": "duplicate", "message_id": 7102},
    )
    sleeps = []

    result = _deliver_with_template(db, control, sleeps)

    assert result.outcome == "reconciled"
    assert [name for name, _ in db.calls] == ["reserve", "authorize", "finalize"]
    assert db.calls[-1][1]["chatwoot_message_id"] == 7102


def test_a_template_blocked_after_the_text_is_reported_for_handoff() -> None:
    db = FakeSupabase()
    control = TemplateControl(
        _catalog(), second={"status": "blocked", "reason": "conversation_advanced"}
    )
    sleeps = []

    result = _deliver_with_template(db, control, sleeps)

    # app.py deriva a una persona con este motivo: el texto salio, el link no.
    assert result.outcome == "blocked"
    assert result.reason == "conversation_advanced"
    assert len(_sends(control)) == 2
    assert [name for name, _ in db.calls] == ["reserve"]
