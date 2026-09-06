import asyncio
from dataclasses import replace
from datetime import UTC, datetime
import hashlib
import hmac
import json
import time
from pathlib import Path

import httpx
import pytest

from bridge.app import Settings, create_app
from bridge.commercial_ally import JOHANNA_COMMERCIAL_ALLY
from bridge.supabase import (
    CommercialAllyPostInboundDiscountPlan,
    InboundCommercialCaseAdmissionResult,
    SupabaseClient,
)


ALLOWED_JID = "12025550123@s.whatsapp.net"


class StubSupabase:
    def __init__(self, *, discount_outcome: str = "created") -> None:
        self.calls: list[dict[str, object]] = []
        self.discount_calls: list[dict[str, object]] = []
        self.discount_outcome = discount_outcome

    async def admit_inbound_commercial_case(
        self, **kwargs: object
    ) -> InboundCommercialCaseAdmissionResult:
        self.calls.append(kwargs)
        return InboundCommercialCaseAdmissionResult(
            outcome="created",
            commercial_case_id="case-1",
            contact_id="contact-1",
            channel_identity_id="identity-1",
            conversation_id="conversation-1",
            automation_status="draft_only",
        )

    async def plan_commercial_ally_post_inbound_discount(
        self, **kwargs: object
    ) -> CommercialAllyPostInboundDiscountPlan:
        self.discount_calls.append(kwargs)
        created = self.discount_outcome == "created"
        return CommercialAllyPostInboundDiscountPlan(
            outcome=self.discount_outcome,
            recovery_case_id="recovery-1" if created else None,
            scheduled_action_id="discount-action-1" if created else None,
            inbound_message_id="inbound-message-1" if created else None,
        )


class StubShadowProcessor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def run(
        self, *, delivery_id: str, context: dict[str, object]
    ) -> None:
        self.calls.append((delivery_id, context))

    def record_failure(self, *, delivery_id: str, reason: str) -> None:
        raise AssertionError("Cut B admission must not invoke Hermes")

    def has_result(self, *, delivery_id: str) -> bool:
        return False

    def get_completed_proposal(
        self, *, delivery_id: str
    ) -> dict[str, object] | None:
        return None


def _headers(raw_body: bytes, *, delivery: str) -> dict[str, str]:
    timestamp = str(int(time.time()))
    signature = "sha256=" + hmac.new(
        b"webhook-secret",
        timestamp.encode("ascii") + b"." + raw_body,
        hashlib.sha256,
    ).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Chatwoot-Signature": signature,
        "X-Chatwoot-Timestamp": timestamp,
        "X-Chatwoot-Delivery": delivery,
    }


def test_supabase_admits_one_inbound_commercial_case() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=[
                {
                    "outcome": "created",
                    "commercial_case_id": "case-1",
                    "contact_id": "contact-1",
                    "channel_identity_id": "identity-1",
                    "conversation_id": "conversation-1",
                    "automation_status": "draft_only",
                }
            ],
        )

    client = SupabaseClient(
        base_url="https://example.supabase.co",
        service_role_key="service-role",
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(
        client.admit_inbound_commercial_case(
            scope_key="libre-de-ansiedad-inbound",
            scope_version=1,
            external_conversation_id=123,
            external_user_id="12025550123",
        )
    )

    assert result.outcome == "created"
    assert result.automation_status == "draft_only"
    assert len(requests) == 1
    assert requests[0].url.path.endswith(
        "/rest/v1/rpc/admit_inbound_commercial_case_v2"
    )
    assert json.loads(requests[0].content) == {
        "p_scope_key": "libre-de-ansiedad-inbound",
        "p_scope_version": 1,
        "p_external_conversation_id": 123,
        "p_external_user_id": "12025550123",
    }


def test_supabase_accepts_durable_blocked_inbound_replay() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "outcome": "blocked",
                    "commercial_case_id": "case-1",
                    "contact_id": "contact-1",
                    "channel_identity_id": "identity-1",
                    "conversation_id": "conversation-1",
                    "automation_status": "disabled",
                }
            ],
        )

    client = SupabaseClient(
        base_url="https://example.supabase.co",
        service_role_key="service-role",
        transport=httpx.MockTransport(handler),
    )

    result = asyncio.run(
        client.admit_inbound_commercial_case(
            scope_key="libre-de-ansiedad-inbound",
            scope_version=2,
            external_conversation_id=123,
            external_user_id="12025550123",
        )
    )

    assert result.outcome == "blocked"
    assert result.automation_status == "disabled"


def test_enabled_cut_b_admits_scoped_inbound_without_invoking_hermes(
    tmp_path: Path,
) -> None:
    supabase = StubSupabase()
    shadow = StubShadowProcessor()
    app = create_app(
        Settings(
            webhook_secret="webhook-secret",
            allowed_jid=ALLOWED_JID,
            capture_dir=tmp_path,
            max_age_seconds=300,
            chatwoot_account_id=1,
            chatwoot_inbox_id=7,
            chatwoot_cut_b_admission_enabled=True,
            chatwoot_cut_b_scope_key="libre-de-ansiedad-inbound",
            chatwoot_cut_b_scope_version=1,
        ),
        supabase_client=supabase,  # type: ignore[arg-type]
        shadow_processor=shadow,
    )
    payload = {
        "event": "message_created",
        "id": 789,
        "content": "Hola",
        "message_type": "incoming",
        "private": False,
        "account": {"id": 1},
        "inbox": {"id": 7},
        "conversation": {
            "id": 123,
            "inbox_id": 7,
            "contact_inbox": {"source_id": ALLOWED_JID},
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode()

    async def exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.post(
                "/webhooks/chatwoot",
                content=raw_body,
                headers=_headers(raw_body, delivery="cut-b-delivery"),
            )
        await app.state.chatwoot_worker.run_once()
        return response

    response = asyncio.run(exercise())

    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    assert supabase.calls == [
        {
            "scope_key": "libre-de-ansiedad-inbound",
            "scope_version": 1,
            "external_conversation_id": 123,
            "external_user_id": "12025550123",
        }
    ]
    assert shadow.calls == []


@pytest.mark.parametrize("discount_outcome", [
    "created",
    "recovery_case_not_applicable",
])
def test_enabled_post_inbound_discount_plans_without_generic_case_or_agent(
    tmp_path: Path,
    discount_outcome: str,
) -> None:
    supabase = StubSupabase(discount_outcome=discount_outcome)
    created_at = int(time.time())
    ally = replace(
        JOHANNA_COMMERCIAL_ALLY,
        tenant_ref="att1",
        funnel_ref="att1-main",
        binding_version=1,
        chatwoot_account_id=2,
        chatwoot_inbox_id=7,
        inbound_scope_key="att1-inbound",
        inbound_scope_version=1,
    )
    app = create_app(
        Settings(
            webhook_secret="webhook-secret",
            allowed_jid=ALLOWED_JID,
            capture_dir=tmp_path,
            max_age_seconds=300,
            commercial_ally_config=ally,
            commercial_ally_manifest_path=tmp_path / "att1-manifest.json",
            chatwoot_account_id=2,
            chatwoot_inbox_id=7,
            chatwoot_cut_b_admission_enabled=True,
            chatwoot_cut_b_scope_key="att1-inbound",
            chatwoot_cut_b_scope_version=1,
            chatwoot_post_inbound_discount_planning_enabled=True,
            commercial_ally_discount_policy_key="att1-recovery-triplet",
            commercial_ally_discount_policy_version=1,
            supabase_base_url="https://example.supabase.co",
            supabase_service_role_key="service-role",
        ),
        supabase_client=supabase,  # type: ignore[arg-type]
    )
    payload = {
        "event": "message_created",
        "id": 9002,
        "created_at": created_at,
        "content": "Sí",
        "message_type": "incoming",
        "private": False,
        "account": {"id": 2},
        "inbox": {"id": 7},
        "conversation": {
            "id": 9001,
            "inbox_id": 7,
            "contact_inbox": {"source_id": ALLOWED_JID},
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode()

    async def exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.post(
                "/webhooks/chatwoot",
                content=raw_body,
                headers=_headers(raw_body, delivery="discount-inbound-delivery"),
            )
        await app.state.chatwoot_worker.run_once()
        return response

    response = asyncio.run(exercise())

    assert response.status_code == 202
    assert supabase.calls == []
    assert supabase.discount_calls == [{
        "tenant_ref": "att1",
        "funnel_ref": "att1-main",
        "binding_version": 1,
        "discount_policy_key": "att1-recovery-triplet",
        "discount_policy_version": 1,
        "chatwoot_account_id": 2,
        "chatwoot_inbox_id": 7,
        "chatwoot_conversation_id": 9001,
        "chatwoot_message_id": 9002,
        "external_user_id": "12025550123",
        "inbound_received_at": datetime.fromtimestamp(
            created_at, tz=UTC
        ).isoformat(),
    }]


def test_post_inbound_discount_planning_rejects_non_att1_manifest(
    tmp_path: Path,
) -> None:
    ally = replace(
        JOHANNA_COMMERCIAL_ALLY,
        tenant_ref="foreign",
        funnel_ref="foreign-main",
        binding_version=1,
        chatwoot_account_id=2,
        chatwoot_inbox_id=7,
        inbound_scope_key="foreign-inbound",
        inbound_scope_version=1,
    )
    with pytest.raises(ValueError, match="ATT1"):
        create_app(
            Settings(
                webhook_secret="webhook-secret",
                allowed_jid=ALLOWED_JID,
                capture_dir=tmp_path,
                max_age_seconds=300,
                commercial_ally_config=ally,
                commercial_ally_manifest_path=tmp_path / "foreign-manifest.json",
                chatwoot_account_id=2,
                chatwoot_inbox_id=7,
                chatwoot_cut_b_admission_enabled=True,
                chatwoot_cut_b_scope_key="foreign-inbound",
                chatwoot_cut_b_scope_version=1,
                chatwoot_post_inbound_discount_planning_enabled=True,
                commercial_ally_discount_policy_key="foreign-discount",
                commercial_ally_discount_policy_version=1,
                supabase_base_url="https://example.supabase.co",
                supabase_service_role_key="service-role",
            )
        )


def test_post_inbound_discount_planning_rejects_cut_b_agent(
    tmp_path: Path,
) -> None:
    ally = replace(
        JOHANNA_COMMERCIAL_ALLY,
        tenant_ref="att1",
        funnel_ref="att1-main",
        binding_version=1,
        chatwoot_account_id=2,
        chatwoot_inbox_id=7,
        inbound_scope_key="att1-inbound",
        inbound_scope_version=1,
    )
    with pytest.raises(
        ValueError,
        match="post-inbound discount planning cannot enable the Cut B agent",
    ):
        create_app(
            Settings(
                webhook_secret="webhook-secret",
                allowed_jid=ALLOWED_JID,
                capture_dir=tmp_path,
                max_age_seconds=300,
                commercial_ally_config=ally,
                commercial_ally_manifest_path=tmp_path / "att1-manifest.json",
                chatwoot_account_id=2,
                chatwoot_inbox_id=7,
                chatwoot_cut_b_admission_enabled=True,
                chatwoot_cut_b_scope_key="att1-inbound",
                chatwoot_cut_b_scope_version=1,
                chatwoot_cut_b_agent_enabled=True,
                chatwoot_post_inbound_discount_planning_enabled=True,
                commercial_ally_discount_policy_key="att1-recovery-triplet",
                commercial_ally_discount_policy_version=1,
                supabase_base_url="https://example.supabase.co",
                supabase_service_role_key="service-role",
            )
        )


def test_enabled_cut_b_admits_scoped_attachment_without_text(
    tmp_path: Path,
) -> None:
    supabase = StubSupabase()
    app = create_app(
        Settings(
            webhook_secret="webhook-secret",
            allowed_jid=ALLOWED_JID,
            capture_dir=tmp_path,
            max_age_seconds=300,
            chatwoot_account_id=1,
            chatwoot_inbox_id=7,
            chatwoot_cut_b_admission_enabled=True,
            chatwoot_cut_b_scope_key="libre-de-ansiedad-inbound",
            chatwoot_cut_b_scope_version=1,
        ),
        supabase_client=supabase,  # type: ignore[arg-type]
    )
    payload = {
        "event": "message_created",
        "id": 790,
        "content": None,
        "attachments": [{"file_type": "image"}],
        "message_type": "incoming",
        "private": False,
        "account": {"id": 1},
        "inbox": {"id": 7},
        "conversation": {
            "id": 124,
            "inbox_id": 7,
            "contact_inbox": {"source_id": ALLOWED_JID},
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode()

    async def exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.post(
                "/webhooks/chatwoot",
                content=raw_body,
                headers=_headers(raw_body, delivery="cut-b-attachment"),
            )
        await app.state.chatwoot_worker.run_once()
        return response

    response = asyncio.run(exercise())

    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    assert supabase.calls[0]["external_conversation_id"] == 124


@pytest.mark.parametrize(
    ("allowed_jid", "scope_key"),
    [
        ("not-a-whatsapp-jid", "libre-de-ansiedad-inbound"),
        (ALLOWED_JID, "BAD SCOPE!"),
    ],
)
def test_enabled_cut_b_rejects_noncanonical_identity_configuration(
    tmp_path: Path,
    allowed_jid: str,
    scope_key: str,
) -> None:
    with pytest.raises(
        ValueError,
        match="CHATWOOT_CUT_B_ADMISSION_ENABLED requires canonical",
    ):
        create_app(
            Settings(
                webhook_secret="webhook-secret",
                allowed_jid=allowed_jid,
                capture_dir=tmp_path,
                max_age_seconds=300,
                chatwoot_account_id=1,
                chatwoot_inbox_id=7,
                chatwoot_cut_b_admission_enabled=True,
                chatwoot_cut_b_scope_key=scope_key,
                chatwoot_cut_b_scope_version=1,
            ),
            supabase_client=StubSupabase(),  # type: ignore[arg-type]
        )
