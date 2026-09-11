from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
import json
import stat

import httpx
import pytest

from bridge.daily_feedback import (
    DailyFeedbackBatchStore,
    RealConversationBatchGrant,
    ReviewAuthorizationError,
    ReviewPrincipal,
)
from bridge.daily_feedback_export import (
    ChatwootDailyCollector,
    ConversationCollectionError,
    DailyReviewPackage,
    RealConversationSecurityPolicy,
    ReviewConversation,
    ReviewMessage,
    materialize_daily_review_package,
    purge_expired_review_bundle,
    render_review_html,
    run_approval_cli,
    run_cli,
    write_private_review_bundle,
)


def _security_policy() -> RealConversationSecurityPolicy:
    return RealConversationSecurityPolicy(
        real_collection_enabled=True,
        storage_encryption_verified=True,
        retention_hours=72,
        deletion_owner="privacy-operator",
    )


def _minimal_review_package(
    *,
    release_id: str = "release_lineage_unavailable",
    release_version: int = 0,
) -> DailyReviewPackage:
    window_start = datetime(2026, 9, 9, tzinfo=UTC)
    return DailyReviewPackage(
        schema_version="daily-feedback-review-package-v1",
        tenant_ref="tenant-johanna",
        scope_ref="scope-agent-bot-19",
        window_start=window_start,
        window_end=window_start + timedelta(days=1),
        conversations=(
            ReviewConversation(
                conversation_ref="conv_opaque_a",
                display_label="Conversación 01",
                messages=(
                    ReviewMessage(
                        "msg_opaque_1",
                        "prospect",
                        "¿Qué incluye el programa?",
                        window_start + timedelta(hours=1),
                    ),
                    ReviewMessage(
                        "msg_opaque_2",
                        "agent",
                        "Incluye tres fases.",
                        window_start + timedelta(hours=1, minutes=1),
                    ),
                ),
                apparent_objective="Revisar la respuesta del agente",
                observed_outcome="Esperando respuesta del prospecto",
                release_id=release_id,
                release_version=release_version,
            ),
        ),
        retention_hours=72,
        deletion_owner="privacy-operator",
        storage_encryption_verified=True,
    )


def _real_batch_grant() -> RealConversationBatchGrant:
    return RealConversationBatchGrant(
        tenant_id="tenant-johanna",
        scope_id="scope-agent-bot-19",
        reviewer_id="reviewer-juan",
        reviewer_binding_id="binding-johanna-owner-v1",
        package_schema_version="daily-feedback-review-package-v1",
        sanitizer_version="deterministic-redaction-v1",
        selection_version="chatwoot-daily-agent-dialogues-v1",
        retention_hours=72,
        deletion_owner="privacy-operator",
        storage_encryption_evidence_ref="enc-evidence-prod-volume-v1",
        active=True,
    )


def test_materializes_minimized_package_into_authoritative_batch(tmp_path: Path) -> None:
    window_start = datetime(2026, 9, 9, tzinfo=UTC)
    window_end = datetime(2026, 9, 10, tzinfo=UTC)
    package = DailyReviewPackage(
        schema_version="daily-feedback-review-package-v1",
        tenant_ref="tenant-johanna",
        scope_ref="scope-agent-bot-19",
        window_start=window_start,
        window_end=window_end,
        conversations=(
            ReviewConversation(
                conversation_ref="conv_opaque_a",
                display_label="Conversación 01",
                messages=(
                    ReviewMessage(
                        message_ref="msg_opaque_1",
                        actor="prospect",
                        text="¿Qué incluye el programa?",
                        occurred_at=window_start + timedelta(hours=1),
                    ),
                    ReviewMessage(
                        message_ref="msg_opaque_2",
                        actor="agent",
                        text="Incluye tres fases.",
                        occurred_at=window_start + timedelta(hours=1, minutes=1),
                    ),
                ),
                apparent_objective="Revisar la respuesta del agente",
                observed_outcome="Esperando respuesta del prospecto",
                release_id="release_lineage_unavailable",
                release_version=0,
            ),
        ),
        retention_hours=72,
        deletion_owner="privacy-operator",
        storage_encryption_verified=True,
    )
    store = DailyFeedbackBatchStore(tmp_path / "durable")

    created = materialize_daily_review_package(
        store=store,
        command_id="materialize-2026-09-09",
        package=package,
        authority=_real_batch_grant(),
    )

    authority = store.get_review_batch_authority(created.batch.batch_id)
    assert authority.tenant_id == "tenant-johanna"
    assert authority.scope_id == "scope-agent-bot-19"
    assert authority.reviewer_id == "reviewer-juan"
    assert authority.reviewer_binding_id == "binding-johanna-owner-v1"
    assert authority.source_kind == "canonical_minimized_conversation"
    assert authority.window_start == window_start
    assert authority.window_end == window_end
    assert authority.retention_expires_at == window_end + timedelta(hours=72)
    assert authority.deletion_owner == "privacy-operator"
    assert authority.storage_encryption_verified is True

    reviewer = ReviewPrincipal(
        "reviewer-juan", "binding-johanna-owner-v1", "session-1", True
    )
    claim = store.claim_review_session(
        command_id="claim-materialized",
        batch_id=created.batch.batch_id,
        principal=reviewer,
        expected_batch_revision=1,
        lease_seconds=120,
        now=window_end,
    )
    item = store.get_next_review_item(
        batch_id=created.batch.batch_id,
        principal=reviewer,
        session_fence=claim.session_fence,
        now=window_end,
    )
    assert item.fixture_id == "conv_opaque_a"
    assert item.source_kind == "canonical_minimized_conversation"
    assert item.sanitizer_version == "deterministic-redaction-v1"
    assert item.selection_version == "chatwoot-daily-agent-dialogues-v1"
    assert [
        (message.message_ref, message.actor, message.text, message.occurred_at)
        for message in item.messages
    ] == [
        (
            "msg_opaque_1",
            "prospect",
            "¿Qué incluye el programa?",
            window_start + timedelta(hours=1),
        ),
        (
            "msg_opaque_2",
            "agent",
            "Incluye tres fases.",
            window_start + timedelta(hours=1, minutes=1),
        ),
    ]


def test_materialization_rejects_text_that_bypassed_sanitizer(tmp_path: Path) -> None:
    window_start = datetime(2026, 9, 9, tzinfo=UTC)
    package = DailyReviewPackage(
        schema_version="daily-feedback-review-package-v1",
        tenant_ref="tenant-johanna",
        scope_ref="scope-agent-bot-19",
        window_start=window_start,
        window_end=window_start + timedelta(days=1),
        conversations=(
            ReviewConversation(
                conversation_ref="conv_opaque_a",
                display_label="Conversación 01",
                messages=(
                    ReviewMessage(
                        "msg_opaque_1",
                        "prospect",
                        "Escríbeme a raw-person@example.com",
                        window_start + timedelta(hours=1),
                    ),
                    ReviewMessage(
                        "msg_opaque_2",
                        "agent",
                        "Claro.",
                        window_start + timedelta(hours=1, minutes=1),
                    ),
                ),
                apparent_objective="Revisar la respuesta del agente",
                observed_outcome="Esperando respuesta del prospecto",
                release_id="release_lineage_unavailable",
                release_version=0,
            ),
        ),
        retention_hours=72,
        deletion_owner="privacy-operator",
        storage_encryption_verified=True,
    )

    with pytest.raises(
        ConversationCollectionError,
        match="review_package_sanitization_mismatch",
    ):
        materialize_daily_review_package(
            store=DailyFeedbackBatchStore(tmp_path / "durable"),
            command_id="materialize-unsafe",
            package=package,
            authority=_real_batch_grant(),
        )


def test_materialization_rejects_unproven_release_lineage(tmp_path: Path) -> None:
    with pytest.raises(
        ConversationCollectionError,
        match="review_package_release_lineage_unavailable_required",
    ):
        materialize_daily_review_package(
            store=DailyFeedbackBatchStore(tmp_path / "durable"),
            command_id="materialize-unproven-release",
            package=_minimal_review_package(
                release_id="caller-claimed-release",
                release_version=3,
            ),
            authority=_real_batch_grant(),
        )


def test_materialization_rejects_unsanitized_metadata(tmp_path: Path) -> None:
    package = _minimal_review_package()
    unsafe_conversation = replace(
        package.conversations[0],
        apparent_objective="Escribir a raw-person@example.com",
    )

    with pytest.raises(
        ConversationCollectionError,
        match="review_package_sanitization_mismatch",
    ):
        materialize_daily_review_package(
            store=DailyFeedbackBatchStore(tmp_path / "durable"),
            command_id="materialize-unsafe-metadata",
            package=replace(package, conversations=(unsafe_conversation,)),
            authority=_real_batch_grant(),
        )


def test_expired_minimized_transcript_cannot_be_read(tmp_path: Path) -> None:
    package = _minimal_review_package()
    store = DailyFeedbackBatchStore(tmp_path / "durable")
    created = materialize_daily_review_package(
        store=store,
        command_id="materialize-expiring",
        package=package,
        authority=_real_batch_grant(),
    )
    expires_at = package.window_end + timedelta(hours=72)
    reviewer = ReviewPrincipal(
        "reviewer-juan", "binding-johanna-owner-v1", "session-expiring", True
    )
    claim = store.claim_review_session(
        command_id="claim-expiring",
        batch_id=created.batch.batch_id,
        principal=reviewer,
        expected_batch_revision=1,
        lease_seconds=120,
        now=expires_at - timedelta(seconds=1),
    )

    with pytest.raises(ReviewAuthorizationError, match="review_content_expired"):
        store.get_next_review_item(
            batch_id=created.batch.batch_id,
            principal=reviewer,
            session_fence=claim.session_fence,
            now=expires_at,
        )


def test_collects_all_short_message_pages_until_observable_empty_page() -> None:
    window_start = datetime(2026, 9, 9, tzinfo=UTC)
    window_end = datetime(2026, 9, 10, tzinfo=UTC)
    message_requests: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/conversations"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "meta": {"all_count": 1},
                        "payload": [
                            {
                                "id": 501,
                                "inbox_id": 77,
                                "status": "resolved",
                                "updated_at": int(
                                    datetime(2026, 9, 9, 22, tzinfo=UTC).timestamp()
                                ),
                                "meta": {
                                    "assignee": {
                                        "type": "AgentBot",
                                        "id": 19,
                                        "name": "Johanna",
                                    },
                                    "sender": {"id": 901, "name": "Persona"},
                                },
                            }
                        ],
                    }
                },
            )
        before = request.url.params.get("before")
        message_requests.append(before)
        if before is None:
            payload = [
                {
                    "id": 702,
                    "message_type": 1,
                    "content_type": "text",
                    "content": "Respuesta segura",
                    "created_at": int(
                        datetime(2026, 9, 9, 20, tzinfo=UTC).timestamp()
                    ),
                    "sender": {"type": "agent_bot", "id": 19, "name": "Johanna"},
                    "status": "delivered",
                    "private": False,
                }
            ]
        elif before == "702":
            payload = [
                {
                    "id": 701,
                    "message_type": 0,
                    "content_type": "text",
                    "content": "Pregunta segura",
                    "created_at": int(
                        datetime(2026, 9, 9, 19, tzinfo=UTC).timestamp()
                    ),
                    "sender": {"type": "contact", "id": 901, "name": "Persona"},
                    "status": "delivered",
                    "private": False,
                }
            ]
        else:
            payload = []
        return httpx.Response(200, json={"payload": payload})

    collector = ChatwootDailyCollector(
        base_url="https://chatwoot.example.test",
        account_id=44,
        inbox_id=77,
        agent_bot_id=19,
        access_token="not-a-real-token",
        pseudonymization_key=b"0123456789abcdef0123456789abcdef",
        security_policy=_security_policy(),
        transport=httpx.MockTransport(handler),
    )

    package = collector.collect(
        tenant_ref="tenant-johanna",
        scope_ref="scope-libre-ansiedad",
        window_start=window_start,
        window_end=window_end,
    )

    assert message_requests == [None, "702", "701"]
    assert [message.actor for message in package.conversations[0].messages] == [
        "prospect",
        "agent",
    ]


def test_real_collection_verifies_canonical_inbox_and_bound_agent_bot_without_reading_conversations() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        assert request.headers["api_access_token"] == "not-a-real-token"
        if request.url.path == "/api/v1/accounts/44/inboxes/77":
            return httpx.Response(200, json={"id": 77, "account_id": 44})
        if request.url.path == "/api/v1/accounts/44/inboxes/77/agent_bot":
            return httpx.Response(200, json={"agent_bot": {"id": 19}})
        raise AssertionError(f"unexpected request: {request.url.path}")

    collector = ChatwootDailyCollector(
        base_url="https://chatwoot.example.test",
        account_id=44,
        inbox_id=77,
        agent_bot_id=19,
        access_token="not-a-real-token",
        pseudonymization_key=b"k" * 32,
        security_policy=_security_policy(),
        transport=httpx.MockTransport(handler),
    )

    collector.verify_access()

    assert requests == [
        "/api/v1/accounts/44/inboxes/77",
        "/api/v1/accounts/44/inboxes/77/agent_bot",
    ]


def test_real_collection_rejects_wrong_inbox_agent_bot_binding() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/api/v1/accounts/44/inboxes/77":
            return httpx.Response(200, json={"id": 77, "account_id": 44})
        if request.url.path == "/api/v1/accounts/44/inboxes/77/agent_bot":
            return httpx.Response(200, json={"agent_bot": {"id": 20}})
        raise AssertionError(f"unexpected request: {request.url.path}")

    collector = ChatwootDailyCollector(
        base_url="https://chatwoot.example.test",
        account_id=44,
        inbox_id=77,
        agent_bot_id=19,
        access_token="not-a-real-token",
        pseudonymization_key=b"k" * 32,
        security_policy=_security_policy(),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(
        ConversationCollectionError,
        match="chatwoot_scope_verification_failed",
    ):
        collector.verify_access()

    assert requests == [
        "/api/v1/accounts/44/inboxes/77",
        "/api/v1/accounts/44/inboxes/77/agent_bot",
    ]


def test_real_collection_rejects_boolean_provider_ids() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/accounts/1/inboxes/9":
            return httpx.Response(200, json={"id": 9})
        if request.url.path == "/api/v1/accounts/1/inboxes/9/agent_bot":
            return httpx.Response(200, json={"agent_bot": {"id": True}})
        raise AssertionError(f"unexpected request: {request.url.path}")

    collector = ChatwootDailyCollector(
        base_url="https://chatwoot.example.test",
        account_id=1,
        inbox_id=9,
        agent_bot_id=1,
        access_token="not-a-real-token",
        pseudonymization_key=b"k" * 32,
        security_policy=_security_policy(),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(
        ConversationCollectionError,
        match="chatwoot_scope_verification_failed",
    ):
        collector.verify_access()


def test_real_collection_rejects_conflicting_inbox_account_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/accounts/44/inboxes/77":
            return httpx.Response(200, json={"id": 77, "account_id": 999})
        if request.url.path == "/api/v1/accounts/44/inboxes/77/agent_bot":
            return httpx.Response(200, json={"agent_bot": {"id": 19}})
        raise AssertionError(f"unexpected request: {request.url.path}")

    collector = ChatwootDailyCollector(
        base_url="https://chatwoot.example.test",
        account_id=44,
        inbox_id=77,
        agent_bot_id=19,
        access_token="not-a-real-token",
        pseudonymization_key=b"k" * 32,
        security_policy=_security_policy(),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(
        ConversationCollectionError,
        match="chatwoot_scope_verification_failed",
    ):
        collector.verify_access()


def test_real_collection_rejects_non_https_chatwoot_origin() -> None:
    with pytest.raises(
        ConversationCollectionError,
        match="chatwoot_https_origin_required",
    ):
        ChatwootDailyCollector(
            base_url="http://chatwoot.example",
            account_id=7,
            inbox_id=11,
            agent_bot_id=19,
            access_token="token",
            pseudonymization_key=b"k" * 32,
            security_policy=_security_policy(),
        )


def test_real_collection_requires_explicit_storage_and_retention_policy() -> None:
    with pytest.raises(
        ConversationCollectionError,
        match="real_conversation_collection_not_authorized",
    ):
        ChatwootDailyCollector(
            base_url="https://chatwoot.invalid",
            account_id=7,
            inbox_id=11,
            agent_bot_id=19,
            access_token="token",
            pseudonymization_key=b"k" * 32,
            security_policy=RealConversationSecurityPolicy(
                real_collection_enabled=True,
                storage_encryption_verified=False,
                retention_hours=72,
                deletion_owner="privacy-operator",
            ),
        )


def test_collects_sanitizes_and_writes_offline_review_bundle(tmp_path: Path) -> None:
    window_start = datetime(2026, 9, 9, tzinfo=UTC)
    window_end = datetime(2026, 9, 10, tzinfo=UTC)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/accounts/7/conversations":
            assert request.url.params["inbox_id"] == "11"
            assert request.url.params["status"] == "all"
            assert request.url.params["page"] == "1"
            return httpx.Response(
                200,
                json={
                    "data": {
                        "payload": [
                            {
                                "id": 71,
                                "inbox_id": 11,
                                "updated_at": 1788993000,
                                "meta": {"sender": {"name": "Catalina Ochoa"}},
                            }
                        ],
                        "meta": {"current_page": 1, "all_count": 1},
                    }
                },
            )
        if request.url.path == "/api/v1/accounts/7/conversations/71/messages":
            if request.url.params.get("before") is not None:
                return httpx.Response(200, json={"payload": []})
            return httpx.Response(
                200,
                json={
                    "payload": [
                        {
                            "id": 701,
                            "created_at": 1788976800,
                            "message_type": 0,
                            "private": False,
                            "content": (
                                "Hola, soy Catalina. Mi correo es cat@example.com "
                                "y mi teléfono +54 11 5555 2222 "
                                "</script><img src=x onerror=alert(1)> "
                                "javascript:alert(2) \u202e"
                                ),
                            "sender": {"type": "contact", "name": "Catalina Ochoa"},
                            "status": "sent",
                        },
                        {
                            "id": 702,
                            "created_at": 1788976860,
                            "message_type": 1,
                            "private": False,
                            "content": "Puedes verlo en https://example.com/checkout?lead=secret",
                            "sender": {"type": "agent_bot", "id": 19, "name": "Johanna"},
                            "status": "delivered",
                        },
                        {
                            "id": 703,
                            "created_at": 1788976920,
                            "message_type": 1,
                            "private": True,
                            "content": "Nota privada con secreto interno",
                            "sender": {"type": "user", "id": 99},
                            "status": "sent",
                        },
                    ]
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    collector = ChatwootDailyCollector(
        base_url="https://chatwoot.invalid",
        account_id=7,
        inbox_id=11,
        agent_bot_id=19,
        access_token="never-render-this-token",
        pseudonymization_key=b"k" * 32,
        security_policy=_security_policy(),
        transport=httpx.MockTransport(handler),
    )

    package = collector.collect(
        tenant_ref="tenant-johanna",
        scope_ref="scope-libre-ansiedad",
        window_start=window_start,
        window_end=window_end,
    )

    assert package.schema_version == "daily-feedback-review-package-v1"
    assert len(package.conversations) == 1
    conversation = package.conversations[0]
    assert conversation.release_id == "release_lineage_unavailable"
    assert conversation.release_version == 0
    assert conversation.display_label == "Conversación 01"
    assert conversation.conversation_ref.startswith("conv_")
    assert "71" not in conversation.conversation_ref
    assert [message.actor for message in conversation.messages] == [
        "prospect",
        "agent",
    ]
    combined = " ".join(message.text for message in conversation.messages)
    assert "Catalina Ochoa" not in combined
    assert "cat@example.com" not in combined
    assert "+54 11 5555 2222" not in combined
    assert "https://example.com" not in combined
    assert "[NOMBRE]" in combined
    assert "[EMAIL]" in combined
    assert "[TELÉFONO]" in combined
    assert "[ENLACE]" in combined
    assert all("701" not in message.message_ref for message in conversation.messages)

    html = render_review_html(package)
    assert "Conversación 01" in html
    assert "1 de 1" in html
    assert "NO DISTRIBUIR" in html
    assert "Content-Security-Policy" in html
    assert "never-render-this-token" not in html
    assert "Catalina Ochoa" not in html
    assert "Nota privada" not in html
    assert "https://example.com" not in html
    assert "<img src=x" not in html
    assert "javascript:" not in html.lower()
    assert "\u202e" not in html
    assert "&lt;/script&gt;" in html

    result = write_private_review_bundle(
        output_dir=tmp_path / "review",
        package=package,
    )
    assert result.html_path.read_text(encoding="utf-8") == html
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["distribution_ready"] is False
    assert manifest["conversation_count"] == 1
    assert manifest["tenant_ref"] == "tenant-johanna"
    assert manifest["scope_ref"] == "scope-libre-ansiedad"
    assert manifest["activated_changes"] == 0
    assert manifest["html_sha256"].startswith("sha256:")
    assert stat.S_IMODE(result.html_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(result.manifest_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(result.html_path.parent.stat().st_mode) == 0o700
    assert [request.url.path for request in requests] == [
        "/api/v1/accounts/7/conversations",
        "/api/v1/accounts/7/conversations/71/messages",
        "/api/v1/accounts/7/conversations/71/messages",
    ]


def test_accepts_chatwoot_multipage_metadata_without_current_page() -> None:
    requested_pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        requested_pages.append(page)
        start = 1 if page == 1 else 26
        stop = 26 if page == 1 else 48
        return httpx.Response(
            200,
            json={
                "data": {
                    "payload": [
                        {"id": conversation_id, "inbox_id": 11}
                        for conversation_id in range(start, stop)
                    ],
                    "meta": {"all_count": 47},
                }
            },
        )

    collector = ChatwootDailyCollector(
        base_url="https://chatwoot.invalid",
        account_id=7,
        inbox_id=11,
        agent_bot_id=19,
        access_token="token",
        pseudonymization_key=b"k" * 32,
        security_policy=_security_policy(),
        transport=httpx.MockTransport(handler),
    )

    with httpx.Client(
        base_url="https://chatwoot.invalid",
        transport=httpx.MockTransport(handler),
    ) as client:
        conversations = collector._list_conversations(client)

    assert requested_pages == [1, 2]
    assert len(conversations) == 47


@pytest.mark.parametrize("current_page", [None, 0, "1", True])
def test_rejects_invalid_current_page_when_chatwoot_returns_it(
    current_page: object,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "payload": [{"id": 71, "inbox_id": 11}],
                    "meta": {"current_page": current_page, "all_count": 1},
                }
            },
        )

    collector = ChatwootDailyCollector(
        base_url="https://chatwoot.invalid",
        account_id=7,
        inbox_id=11,
        agent_bot_id=19,
        access_token="token",
        pseudonymization_key=b"k" * 32,
        security_policy=_security_policy(),
        transport=httpx.MockTransport(handler),
    )

    with httpx.Client(
        base_url="https://chatwoot.invalid",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(
            ConversationCollectionError,
            match="invalid_chatwoot_conversation_list",
        ):
            collector._list_conversations(client)


def test_fails_closed_when_chatwoot_list_coverage_is_incomplete() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["page"] == "1":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "payload": [{"id": 71, "inbox_id": 11}],
                        "meta": {"current_page": 1, "all_count": 2},
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "payload": [],
                    "meta": {"current_page": 2, "all_count": 2},
                }
            },
        )

    collector = ChatwootDailyCollector(
        base_url="https://chatwoot.invalid",
        account_id=7,
        inbox_id=11,
        agent_bot_id=19,
        access_token="token",
        pseudonymization_key=b"k" * 32,
        security_policy=_security_policy(),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(
        ConversationCollectionError,
        match="chatwoot_conversation_list_incomplete",
    ):
        collector.collect(
            tenant_ref="tenant-johanna",
            scope_ref="scope-libre-ansiedad",
            window_start=datetime(2026, 9, 9, tzinfo=UTC),
            window_end=datetime(2026, 9, 10, tzinfo=UTC),
        )


def test_fails_closed_when_chatwoot_count_changes_between_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/api/v1/accounts/7/conversations":
            raise AssertionError("message collection must not start")
        page = int(request.url.params["page"])
        return httpx.Response(
            200,
            json={
                "data": {
                    "payload": (
                        [{"id": 71, "inbox_id": 11, "updated_at": 1788976800}]
                        if page == 1
                        else []
                    ),
                    "meta": {
                        "current_page": page,
                        "all_count": 2 if page == 1 else 1,
                    },
                }
            },
        )

    collector = ChatwootDailyCollector(
        base_url="https://chatwoot.invalid",
        account_id=7,
        inbox_id=11,
        agent_bot_id=19,
        access_token="token",
        pseudonymization_key=b"k" * 32,
        security_policy=_security_policy(),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(
        ConversationCollectionError,
        match="chatwoot_conversation_count_changed",
    ):
        collector.collect(
            tenant_ref="tenant-johanna",
            scope_ref="scope-libre-ansiedad",
            window_start=datetime(2026, 9, 9, tzinfo=UTC),
            window_end=datetime(2026, 9, 10, tzinfo=UTC),
        )


def test_fails_closed_when_chatwoot_pagination_repeats_a_conversation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/api/v1/accounts/7/conversations":
            raise AssertionError("message collection must not start")
        page = int(request.url.params["page"])
        return httpx.Response(
            200,
            json={
                "data": {
                    "payload": [{"id": 71, "inbox_id": 11}],
                    "meta": {"current_page": page, "all_count": 2},
                }
            },
        )

    collector = ChatwootDailyCollector(
        base_url="https://chatwoot.invalid",
        account_id=7,
        inbox_id=11,
        agent_bot_id=19,
        access_token="token",
        pseudonymization_key=b"k" * 32,
        security_policy=_security_policy(),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(
        ConversationCollectionError,
        match="chatwoot_conversation_pagination_repeated",
    ):
        collector.collect(
            tenant_ref="tenant-johanna",
            scope_ref="scope-libre-ansiedad",
            window_start=datetime(2026, 9, 9, tzinfo=UTC),
            window_end=datetime(2026, 9, 10, tzinfo=UTC),
        )


def test_skips_conversations_with_no_activity_since_window_start() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path != "/api/v1/accounts/7/conversations":
            raise AssertionError("stale conversation history must not be fetched")
        return httpx.Response(
            200,
            json={
                "data": {
                    "payload": [
                        {"id": 71, "inbox_id": 11, "updated_at": 1788825600}
                    ],
                    "meta": {"current_page": 1, "all_count": 1},
                }
            },
        )

    collector = ChatwootDailyCollector(
        base_url="https://chatwoot.invalid",
        account_id=7,
        inbox_id=11,
        agent_bot_id=19,
        access_token="token",
        pseudonymization_key=b"k" * 32,
        security_policy=_security_policy(),
        transport=httpx.MockTransport(handler),
    )
    package = collector.collect(
        tenant_ref="tenant-johanna",
        scope_ref="scope-libre-ansiedad",
        window_start=datetime(2026, 9, 9, tzinfo=UTC),
        window_end=datetime(2026, 9, 10, tzinfo=UTC),
    )

    assert package.conversations == ()
    assert calls == ["/api/v1/accounts/7/conversations"]


def test_cli_reads_secrets_from_environment_and_reports_only_sanitized_state(
    tmp_path: Path,
) -> None:
    package = DailyReviewPackage(
        schema_version="daily-feedback-review-package-v1",
        tenant_ref="tenant-johanna",
        scope_ref="scope-libre-ansiedad",
        window_start=datetime(2026, 9, 9, tzinfo=UTC),
        window_end=datetime(2026, 9, 10, tzinfo=UTC),
        conversations=(),
    )
    captured: dict[str, object] = {}

    class FakeCollector:
        def collect(self, **kwargs: object) -> DailyReviewPackage:
            captured["collect"] = kwargs
            return package

    def factory(**kwargs: object) -> FakeCollector:
        captured["factory"] = kwargs
        return FakeCollector()

    environment = {
        "CHATWOOT_BASE_URL": "https://chatwoot.invalid",
        "CHATWOOT_ACCOUNT_ID": "7",
        "CHATWOOT_INBOX_ID": "11",
        "CHATWOOT_AGENT_BOT_ID": "19",
        "CHATWOOT_API_ACCESS_TOKEN": "super-secret-token",
        "DAILY_FEEDBACK_PSEUDONYMIZATION_KEY": "70" * 32,
        "DAILY_FEEDBACK_REAL_CONVERSATIONS_ENABLED": "true",
        "DAILY_FEEDBACK_STORAGE_ENCRYPTION_VERIFIED": "true",
        "DAILY_FEEDBACK_RETENTION_HOURS": "72",
        "DAILY_FEEDBACK_DELETION_OWNER": "privacy-operator",
    }
    result = run_cli(
        [
            "--tenant-ref",
            "tenant-johanna",
            "--scope-ref",
            "scope-libre-ansiedad",
            "--window-start",
            "2026-09-09T00:00:00Z",
            "--window-end",
            "2026-09-10T00:00:00Z",
            "--output-dir",
            str(tmp_path / "bundle"),
        ],
        environ=environment,
        collector_factory=factory,
    )

    assert result == {
        "status": "created",
        "conversation_count": 0,
        "distribution_ready": False,
        "html_path": str(tmp_path / "bundle" / "review.html"),
        "manifest_path": str(tmp_path / "bundle" / "manifest.json"),
    }
    rendered = json.dumps(result)
    assert "super-secret-token" not in rendered
    assert "70" * 32 not in rendered
    factory_args = captured["factory"]
    assert isinstance(factory_args, dict)
    assert factory_args["access_token"] == "super-secret-token"
    assert factory_args["pseudonymization_key"] == bytes.fromhex("70" * 32)
    collect_args = captured["collect"]
    assert isinstance(collect_args, dict)
    assert "release_id" not in collect_args
    assert "release_version" not in collect_args


def test_writer_publishes_nothing_when_second_artifact_fails(tmp_path: Path) -> None:
    package = DailyReviewPackage(
        schema_version="daily-feedback-review-package-v1",
        tenant_ref="tenant-johanna",
        scope_ref="scope-libre-ansiedad",
        window_start=datetime(2026, 9, 9, tzinfo=UTC),
        window_end=datetime(2026, 9, 10, tzinfo=UTC),
        conversations=(),
        retention_hours=24,
        deletion_owner="privacy-operator",
        storage_encryption_verified=True,
    )
    bundle_dir = tmp_path / "bundle"

    def fail_after_html() -> None:
        raise RuntimeError("injected_manifest_failure")

    with pytest.raises(RuntimeError, match="injected_manifest_failure"):
        write_private_review_bundle(
            output_dir=bundle_dir,
            package=package,
            _after_html_write=fail_after_html,
        )

    assert not bundle_dir.exists()
    assert list(tmp_path.iterdir()) == []


def test_writer_refuses_a_nonempty_bundle_directory(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir(mode=0o700)
    (bundle_dir / "unrelated.txt").write_text("do not absorb", encoding="utf-8")
    package = DailyReviewPackage(
        schema_version="daily-feedback-review-package-v1",
        tenant_ref="tenant-johanna",
        scope_ref="scope-libre-ansiedad",
        window_start=datetime(2026, 9, 9, tzinfo=UTC),
        window_end=datetime(2026, 9, 10, tzinfo=UTC),
        conversations=(),
        retention_hours=24,
        deletion_owner="privacy-operator",
        storage_encryption_verified=True,
    )

    with pytest.raises(
        ConversationCollectionError,
        match="review_output_directory_not_empty",
    ):
        write_private_review_bundle(output_dir=bundle_dir, package=package)

    assert (bundle_dir / "unrelated.txt").read_text(encoding="utf-8") == "do not absorb"
    assert sorted(path.name for path in bundle_dir.iterdir()) == ["unrelated.txt"]


def test_expired_private_bundle_is_deleted_by_retention_guard(tmp_path: Path) -> None:
    created_at = datetime(2026, 9, 10, 12, tzinfo=UTC)
    package = DailyReviewPackage(
        schema_version="daily-feedback-review-package-v1",
        tenant_ref="tenant-johanna",
        scope_ref="scope-libre-ansiedad",
        window_start=datetime(2026, 9, 9, tzinfo=UTC),
        window_end=datetime(2026, 9, 10, tzinfo=UTC),
        conversations=(),
        retention_hours=24,
        deletion_owner="privacy-operator",
        storage_encryption_verified=True,
    )
    bundle_dir = tmp_path / "bundle"
    result = write_private_review_bundle(
        output_dir=bundle_dir,
        package=package,
        now=created_at,
    )
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    html_before_approval = result.html_path.read_bytes()
    assert manifest["created_at"] == "2026-09-10T12:00:00Z"
    assert manifest["expires_at"] == "2026-09-11T12:00:00Z"
    assert manifest["distribution_ready"] is False

    approval_result = run_approval_cli(
        [
            "--bundle-dir",
            str(bundle_dir),
            "--expected-html-sha256",
            manifest["html_sha256"],
            "--reviewer-binding-ref",
            "reviewer-johanna-owner",
        ],
        now=created_at + timedelta(hours=1),
    )
    approved_manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert approval_result["status"] == "privacy_reviewed"
    assert approval_result["distribution_ready"] is False
    assert approved_manifest["human_reviewed"] is True
    assert approved_manifest["distribution_ready"] is False
    assert approved_manifest["reviewed_html_sha256"] == manifest["html_sha256"]
    assert approved_manifest["reviewer_binding_ref"] == "reviewer-johanna-owner"
    assert result.html_path.read_bytes() == html_before_approval

    assert purge_expired_review_bundle(
        bundle_dir=bundle_dir,
        now=created_at + timedelta(hours=23),
    ) is False
    assert result.html_path.exists()
    assert purge_expired_review_bundle(
        bundle_dir=bundle_dir,
        now=created_at + timedelta(hours=25),
    ) is True
    assert not bundle_dir.exists()
