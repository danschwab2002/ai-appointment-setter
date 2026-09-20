import asyncio
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
