from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable
import json
import logging
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
    DEFAULT_MAX_CONVERSATION_PAGES,
    PACKAGE_SCHEMA_V2,
    SANITIZER_VERSION_V2,
    SELECTION_VERSION_V2,
    ChatwootDailyCollector,
    ConversationCollectionError,
    DailyReviewPackage,
    RealConversationSecurityPolicy,
    ReviewConversation,
    ReviewMessage,
    apply_conversation_context,
    classify_chatwoot_message,
    materialize_daily_review_package,
    purge_expired_review_bundle,
    render_review_html,
    run_approval_cli,
    run_cli,
    validate_agent_bot_binding,
    validate_max_conversation_pages,
    validate_storage_risk_acceptance_ref,
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
    """Un linaje que el llamador se invento no entra.

    Hasta el 2026-09-28 este guard exigia el literal
    'release_lineage_unavailable' y por lo tanto el campo no podia llevar nada
    real. Desde 20260928000100 la procedencia se registra por turno, asi que el
    guard cambio de forma --- ahora admite el marcador o un digest de verdad ---
    pero lo que protege es lo mismo: un string arbitrario sigue siendo un linaje
    inventado, y eso es peor que no tener linaje.
    """
    with pytest.raises(
        ConversationCollectionError,
        match="review_package_release_lineage_invalid",
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


def test_materialization_accepts_a_real_release_digest(tmp_path: Path) -> None:
    """Un digest de release de verdad entra, que era el punto del cambio.

    Sin esto el guard viejo seguiria vivo con otro nombre: rechazar lo inventado
    no prueba que lo legitimo pase. El digest son los 64 hex que
    `register_agent_prompt_release.py` calcula sobre los artefactos del perfil.
    """
    created = materialize_daily_review_package(
        store=DailyFeedbackBatchStore(tmp_path / "durable"),
        command_id="materialize-real-release",
        package=_minimal_review_package(
            release_id="a" * 64,
            release_version=7,
        ),
        authority=_real_batch_grant(),
    )
    assert created is not None


def test_a_release_version_of_zero_with_a_digest_is_rejected(tmp_path: Path) -> None:
    """Un digest con version 0 es media procedencia, y media no sirve.

    `daily_feedback.py` exige `release_version >= 1` cuando el linaje no es el
    marcador; si el exportador dejara pasar la combinacion, el rechazo llegaria
    una capa mas abajo y con otro mensaje.
    """
    with pytest.raises(
        ConversationCollectionError,
        match="review_package_release_lineage_invalid",
    ):
        materialize_daily_review_package(
            store=DailyFeedbackBatchStore(tmp_path / "durable"),
            command_id="materialize-half-release",
            package=_minimal_review_package(
                release_id="b" * 64,
                release_version=0,
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
                                "updated_at": (
                                    datetime(2026, 9, 9, 22, tzinfo=UTC).timestamp()
                                    + 0.125
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


@pytest.mark.parametrize(
    "updated_at",
    [None, True, "1", 0, -1.0, float("nan"), float("inf")],
)
def test_rejects_invalid_chatwoot_conversation_updated_at(
    updated_at: object,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith("/conversations"):
            raise AssertionError("message collection must not start")
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "data": {
                        "meta": {"all_count": 1},
                        "payload": [
                            {"id": 501, "inbox_id": 77, "updated_at": updated_at}
                        ],
                    }
                }
            ).encode("utf-8"),
            headers={"content-type": "application/json"},
        )

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
        match="chatwoot_conversation_scope_mismatch",
    ):
        collector.collect(
            tenant_ref="tenant-johanna",
            scope_ref="scope-libre-ansiedad",
            window_start=datetime(2026, 9, 9, tzinfo=UTC),
            window_end=datetime(2026, 9, 10, tzinfo=UTC),
        )


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


# La revision diaria en una instancia autohospedada (ATT1).
#
# Las respuestas de Chatwoot son capturas reales del 10/10/2026, con lista blanca de
# campos y sin tokens: el inbox 11 de ATT1 (cuenta 2), su agent_bot (un objeto vacio:
# el bot no esta vinculado, a proposito), el bot 2 de la cuenta y, como control de un
# inbox con bot, el agent_bot del inbox 9 de Johanna (cuenta 1, bot 1; el vinculo se
# paso a inactive el 23/09 y la respuesta no dice el estado). El show del inbox 9 es
# la captura del 23/09.

FIXTURES = Path(__file__).parent / "fixtures"
ATT1_RISK_ACCEPTANCE_REF = "https://example.test/instancia-att1/aceptacion-de-riesgo.md"


def _capture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text("utf-8"))


ATT1_INBOX = _capture("chatwoot_inbox_11_show_20261010.json")
ATT1_INBOX_AGENT_BOT = _capture("chatwoot_inbox_11_agent_bot_unlinked_20261010.json")
ATT1_ACCOUNT_AGENT_BOT = _capture("chatwoot_account_2_agent_bot_2_20261010.json")
JOHANNA_INBOX_AGENT_BOT = _capture("chatwoot_inbox_9_agent_bot_linked_20261010.json")
JOHANNA_INBOX = {
    key: value
    for key, value in _capture("chatwoot_inbox_9_message_templates_20260923.json").items()
    if key != "_fixture"
}
ATT1_INBOX_PATH = "/api/v1/accounts/2/inboxes/11"
ATT1_INBOX_AGENT_BOT_PATH = "/api/v1/accounts/2/inboxes/11/agent_bot"
ATT1_ACCOUNT_AGENT_BOT_PATH = "/api/v1/accounts/2/agent_bots/2"
JOHANNA_INBOX_PATH = "/api/v1/accounts/1/inboxes/9"
JOHANNA_INBOX_AGENT_BOT_PATH = "/api/v1/accounts/1/inboxes/9/agent_bot"


def _att1_security_policy(**changes: object) -> RealConversationSecurityPolicy:
    return replace(
        RealConversationSecurityPolicy(
            real_collection_enabled=True,
            storage_encryption_verified=False,
            retention_hours=72,
            deletion_owner="att1-joint-reviewer-accountability-v1",
            storage_risk_acceptance_ref=ATT1_RISK_ACCEPTANCE_REF,
        ),
        **changes,
    )


def _chatwoot(
    routes: dict[str, tuple[int, object]], requests: list[str]
) -> httpx.MockTransport:
    """Chatwoot falso: sirve cada ruta y anota el orden de los pedidos. Un cuerpo en
    bytes se manda tal cual (un JSON roto)."""

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        assert request.headers["api_access_token"] == "not-a-real-token"
        if request.url.path not in routes:
            raise AssertionError(f"unexpected request: {request.url.path}")
        status, body = routes[request.url.path]
        if isinstance(body, bytes):
            return httpx.Response(
                status, content=body, headers={"content-type": "application/json"}
            )
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


def _att1_routes(
    *,
    inbox_agent_bot: tuple[int, object] | None = None,
    account_agent_bot: tuple[int, object] | None = None,
) -> dict[str, tuple[int, object]]:
    return {
        ATT1_INBOX_PATH: (ATT1_INBOX["status"], ATT1_INBOX["body"]),
        ATT1_INBOX_AGENT_BOT_PATH: inbox_agent_bot
        or (ATT1_INBOX_AGENT_BOT["status"], ATT1_INBOX_AGENT_BOT["body"]),
        ATT1_ACCOUNT_AGENT_BOT_PATH: account_agent_bot
        or (ATT1_ACCOUNT_AGENT_BOT["status"], ATT1_ACCOUNT_AGENT_BOT["body"]),
    }


def _att1_collector(
    transport: httpx.BaseTransport,
    *,
    agent_bot_binding: object = "unlinked",
    security_policy: RealConversationSecurityPolicy | None = None,
    max_conversation_pages: object = DEFAULT_MAX_CONVERSATION_PAGES,
) -> ChatwootDailyCollector:
    return ChatwootDailyCollector(
        base_url="https://chatwoot.example.test",
        account_id=2,
        inbox_id=11,
        agent_bot_id=2,
        access_token="not-a-real-token",
        pseudonymization_key=b"k" * 32,
        security_policy=security_policy or _att1_security_policy(),
        transport=transport,
        max_conversation_pages=max_conversation_pages,  # type: ignore[arg-type]
        package_version=2,
        agent_bot_binding=agent_bot_binding,  # type: ignore[arg-type]
    )


def _johanna_collector(
    transport: httpx.BaseTransport, **binding: str
) -> ChatwootDailyCollector:
    return ChatwootDailyCollector(
        base_url="https://chatwoot.example.test",
        account_id=1,
        inbox_id=9,
        agent_bot_id=1,
        access_token="not-a-real-token",
        pseudonymization_key=b"k" * 32,
        security_policy=_security_policy(),
        transport=transport,
        package_version=2,
        **binding,
    )


def test_unlinked_binding_verifies_against_captured_att1_responses() -> None:
    # Lo que fija la captura: el show del inbox no trae account_id y su agent_bot es
    # {} (sin bot); el bot 2 es de la cuenta 2.
    assert "account_id" not in ATT1_INBOX["body"]
    assert ATT1_INBOX_AGENT_BOT["body"] == {}
    requests: list[str] = []
    collector = _att1_collector(_chatwoot(_att1_routes(), requests))

    collector.verify_access()

    assert collector.agent_bot_binding == "unlinked"
    assert requests == [
        ATT1_INBOX_PATH,
        ATT1_INBOX_AGENT_BOT_PATH,
        ATT1_ACCOUNT_AGENT_BOT_PATH,
    ]


def test_unlinked_binding_keeps_nothing_of_the_bot_credentials(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # La respuesta real de agent_bots/2 trae access_token y secret (estan en keys; la
    # captura no los guardo). Van con valores testigo, con la forma que documenta la
    # captura del inbox 9, para probar que no quedan en ningun lado.
    assert {"access_token", "secret"} <= set(ATT1_ACCOUNT_AGENT_BOT["keys"])
    assert set(JOHANNA_INBOX_AGENT_BOT["shape"]["agent_bot"]["access_token"]) == {
        "id", "owner_type", "owner_id", "token", "created_at", "updated_at",
    }
    token = "testigo-token-del-bot-que-no-se-guarda"
    secret = "testigo-secret-del-bot-que-no-se-guarda"
    body = {
        **ATT1_ACCOUNT_AGENT_BOT["body"],
        "access_token": {
            "id": 7,
            "owner_type": "AgentBot",
            "owner_id": 2,
            "token": token,
            "created_at": "2026-10-01T00:00:00.000Z",
            "updated_at": "2026-10-01T00:00:00.000Z",
        },
        "secret": secret,
    }
    collector = _att1_collector(
        _chatwoot(_att1_routes(account_agent_bot=(200, body)), [])
    )

    with caplog.at_level(logging.DEBUG):
        collector.verify_access()

    kept = repr(
        {name: value for name, value in vars(collector).items() if name != "_transport"}
    )
    for witness in (token, secret):
        assert witness not in caplog.text
        assert witness not in kept

    # Un JSON roto que trae el token tampoco viaja en la excepcion.
    broken = (
        '{"id": 2, "account_id": 2, "access_token": {"token": "' + token + '"'
    ).encode("utf-8")
    broken_collector = _att1_collector(
        _chatwoot(_att1_routes(account_agent_bot=(200, broken)), [])
    )
    with pytest.raises(
        ConversationCollectionError, match="chatwoot_scope_verification_failed"
    ) as raised:
        broken_collector.verify_access()
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_unlinked_binding_fails_closed_when_the_inbox_has_a_bot() -> None:
    # Johanna con su configuracion y sus capturas: el inbox 9 devuelve el bot 1. Sin
    # vincular, eso no pasa, y el bot de la cuenta (cuya respuesta trae el token) ni
    # se pide.
    requests: list[str] = []
    collector = _johanna_collector(
        _chatwoot(
            {
                JOHANNA_INBOX_PATH: (200, JOHANNA_INBOX),
                JOHANNA_INBOX_AGENT_BOT_PATH: (
                    JOHANNA_INBOX_AGENT_BOT["status"],
                    JOHANNA_INBOX_AGENT_BOT["body"],
                ),
            },
            requests,
        ),
        agent_bot_binding="unlinked",
    )

    with pytest.raises(
        ConversationCollectionError, match="chatwoot_agent_bot_unexpectedly_linked"
    ):
        collector.verify_access()

    assert requests == [JOHANNA_INBOX_PATH, JOHANNA_INBOX_AGENT_BOT_PATH]


@pytest.mark.parametrize(
    "linked",
    [
        pytest.param(JOHANNA_INBOX_AGENT_BOT["body"], id="la-captura-del-inbox-9"),
        pytest.param({"agent_bot": ATT1_ACCOUNT_AGENT_BOT["body"]}, id="el-bot-2-vinculado"),
    ],
)
def test_unlinked_binding_fails_closed_when_att1_inbox_gets_a_bot(
    linked: dict[str, object],
) -> None:
    requests: list[str] = []
    collector = _att1_collector(
        _chatwoot(_att1_routes(inbox_agent_bot=(200, linked)), requests)
    )

    with pytest.raises(
        ConversationCollectionError, match="chatwoot_agent_bot_unexpectedly_linked"
    ):
        collector.verify_access()

    assert requests == [ATT1_INBOX_PATH, ATT1_INBOX_AGENT_BOT_PATH]


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda bot: {**bot, "id": 3}, id="otro-bot"),
        pytest.param(lambda bot: {**bot, "account_id": 1}, id="otra-cuenta"),
        pytest.param(
            lambda bot: {key: value for key, value in bot.items() if key != "account_id"},
            id="sin-cuenta",
        ),
        pytest.param(lambda bot: {**bot, "account_id": None}, id="bot-de-sistema"),
        pytest.param(lambda bot: {**bot, "account_id": "2"}, id="cuenta-como-texto"),
        pytest.param(lambda bot: {**bot, "id": True}, id="id-booleano"),
        # Iguales a los configurados pero no enteros (2.0 == 2 en Python): solo los
        # frena el chequeo de tipo, no la comparacion.
        pytest.param(lambda bot: {**bot, "id": 2.0}, id="id-decimal"),
        pytest.param(lambda bot: {**bot, "account_id": 2.0}, id="cuenta-decimal"),
        pytest.param(lambda bot: [bot], id="no-es-un-objeto"),
    ],
)
def test_unlinked_binding_rejects_another_bot_or_account(
    change: Callable[[dict[str, object]], object],
) -> None:
    requests: list[str] = []
    collector = _att1_collector(
        _chatwoot(
            _att1_routes(account_agent_bot=(200, change(ATT1_ACCOUNT_AGENT_BOT["body"]))),
            requests,
        )
    )

    with pytest.raises(
        ConversationCollectionError, match="chatwoot_scope_verification_failed"
    ):
        collector.verify_access()

    assert requests == [
        ATT1_INBOX_PATH,
        ATT1_INBOX_AGENT_BOT_PATH,
        ATT1_ACCOUNT_AGENT_BOT_PATH,
    ]


# Cada pedido tiene que dar 200. Las respuestas llevan el cuerpo capturado, que con
# un 200 pasaria: lo que se prueba es el status, no la forma del cuerpo.


@pytest.mark.parametrize("status", [401, 404])
def test_unlinked_binding_fails_closed_when_the_account_bot_cannot_be_read(
    status: int,
) -> None:
    requests: list[str] = []
    collector = _att1_collector(
        _chatwoot(
            _att1_routes(account_agent_bot=(status, ATT1_ACCOUNT_AGENT_BOT["body"])),
            requests,
        )
    )

    with pytest.raises(
        ConversationCollectionError, match="^chatwoot_scope_verification_failed$"
    ):
        collector.verify_access()

    assert requests == [
        ATT1_INBOX_PATH,
        ATT1_INBOX_AGENT_BOT_PATH,
        ATT1_ACCOUNT_AGENT_BOT_PATH,
    ]


@pytest.mark.parametrize("status", [401, 404])
def test_unlinked_binding_requires_200_from_the_inbox_agent_bot(status: int) -> None:
    # Un 404 con {} no prueba que el inbox no tenga bot.
    requests: list[str] = []
    collector = _att1_collector(
        _chatwoot(
            _att1_routes(inbox_agent_bot=(status, ATT1_INBOX_AGENT_BOT["body"])),
            requests,
        )
    )

    with pytest.raises(
        ConversationCollectionError, match="^chatwoot_scope_verification_failed$"
    ):
        collector.verify_access()

    assert requests == [ATT1_INBOX_PATH, ATT1_INBOX_AGENT_BOT_PATH]


@pytest.mark.parametrize("status", [401, 404])
def test_both_bindings_require_200_from_the_inbox_show(status: int) -> None:
    # El show del inbox, en los dos modos: ATT1 sin vincular y Johanna con el de
    # siempre. Con el status de error no se pide nada mas.
    att1_requests: list[str] = []
    att1 = _att1_collector(
        _chatwoot(
            {**_att1_routes(), ATT1_INBOX_PATH: (status, ATT1_INBOX["body"])},
            att1_requests,
        )
    )
    johanna_requests: list[str] = []
    johanna = _johanna_collector(
        _chatwoot(
            {
                JOHANNA_INBOX_PATH: (status, JOHANNA_INBOX),
                JOHANNA_INBOX_AGENT_BOT_PATH: (
                    JOHANNA_INBOX_AGENT_BOT["status"],
                    JOHANNA_INBOX_AGENT_BOT["body"],
                ),
            },
            johanna_requests,
        )
    )

    for collector in (att1, johanna):
        with pytest.raises(
            ConversationCollectionError, match="^chatwoot_scope_verification_failed$"
        ):
            collector.verify_access()

    assert att1_requests == [ATT1_INBOX_PATH]
    assert johanna_requests == [JOHANNA_INBOX_PATH]


# La forma capturada es {}; agent_bot null o vacio no se capturaron y se toleran
# porque tampoco traen un bot. Todo lo demas falla cerrado.
@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({}, id="la-captura"),
        pytest.param({"agent_bot": None}, id="agent-bot-null"),
        pytest.param({"agent_bot": {}}, id="agent-bot-vacio"),
    ],
)
def test_unlinked_binding_accepts_the_shapes_without_a_bot(
    payload: dict[str, object],
) -> None:
    requests: list[str] = []
    _att1_collector(
        _chatwoot(_att1_routes(inbox_agent_bot=(200, payload)), requests)
    ).verify_access()

    assert requests == [
        ATT1_INBOX_PATH,
        ATT1_INBOX_AGENT_BOT_PATH,
        ATT1_ACCOUNT_AGENT_BOT_PATH,
    ]


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param([], id="lista"),
        pytest.param(None, id="null"),
        pytest.param({"agent_bot": {"name": "att1-setter"}}, id="bot-sin-id"),
        pytest.param({"agent_bot": "att1-setter"}, id="bot-como-texto"),
        pytest.param({"agent_bot": None, "inbox_id": 11}, id="otra-clave"),
    ],
)
def test_unlinked_binding_fails_closed_on_shapes_that_prove_nothing(
    payload: object,
) -> None:
    requests: list[str] = []
    collector = _att1_collector(
        _chatwoot(_att1_routes(inbox_agent_bot=(200, payload)), requests)
    )

    with pytest.raises(
        ConversationCollectionError, match="chatwoot_scope_verification_failed"
    ):
        collector.verify_access()

    assert ATT1_ACCOUNT_AGENT_BOT_PATH not in requests


def test_unlinked_binding_still_verifies_the_inbox() -> None:
    requests: list[str] = []
    collector = ChatwootDailyCollector(
        base_url="https://chatwoot.example.test",
        account_id=2,
        inbox_id=12,
        agent_bot_id=2,
        access_token="not-a-real-token",
        pseudonymization_key=b"k" * 32,
        security_policy=_att1_security_policy(),
        transport=_chatwoot(
            {
                "/api/v1/accounts/2/inboxes/12": (200, ATT1_INBOX["body"]),
                "/api/v1/accounts/2/inboxes/12/agent_bot": (200, {}),
            },
            requests,
        ),
        agent_bot_binding="unlinked",
    )

    with pytest.raises(
        ConversationCollectionError, match="chatwoot_scope_verification_failed"
    ):
        collector.verify_access()

    assert requests == [
        "/api/v1/accounts/2/inboxes/12",
        "/api/v1/accounts/2/inboxes/12/agent_bot",
    ]


def test_inbox_binding_accepts_johannas_captured_link() -> None:
    # El modo de siempre, con las capturas de Johanna: el endpoint devuelve el bot 1
    # (con el vinculo pasado a inactive el 23/09), y por eso su arranque pasa. Dos
    # pedidos, como hoy.
    requests: list[str] = []
    collector = _johanna_collector(
        _chatwoot(
            {
                JOHANNA_INBOX_PATH: (200, JOHANNA_INBOX),
                JOHANNA_INBOX_AGENT_BOT_PATH: (
                    JOHANNA_INBOX_AGENT_BOT["status"],
                    JOHANNA_INBOX_AGENT_BOT["body"],
                ),
            },
            requests,
        )
    )

    collector.verify_access()

    assert collector.agent_bot_binding == "inbox"
    assert requests == [JOHANNA_INBOX_PATH, JOHANNA_INBOX_AGENT_BOT_PATH]


def test_inbox_binding_still_rejects_the_att1_inbox_without_a_bot() -> None:
    # Por esto ATT1 declara la excepcion: con el modo de siempre su inbox no pasa.
    requests: list[str] = []
    collector = _att1_collector(
        _chatwoot(_att1_routes(), requests),
        agent_bot_binding="inbox",
        security_policy=_security_policy(),
    )

    with pytest.raises(
        ConversationCollectionError, match="chatwoot_scope_verification_failed"
    ):
        collector.verify_access()

    assert requests == [ATT1_INBOX_PATH, ATT1_INBOX_AGENT_BOT_PATH]


@pytest.mark.parametrize("binding", ["", "Unlinked", "unlinked ", "none", None, 1])
def test_agent_bot_binding_accepts_only_inbox_or_unlinked(binding: object) -> None:
    assert validate_agent_bot_binding("inbox") == "inbox"
    assert validate_agent_bot_binding("unlinked") == "unlinked"
    with pytest.raises(
        ValueError, match="invalid_daily_feedback_chatwoot_agent_bot_binding"
    ):
        validate_agent_bot_binding(binding)
    with pytest.raises(
        ValueError, match="invalid_daily_feedback_chatwoot_agent_bot_binding"
    ):
        _att1_collector(_chatwoot({}, []), agent_bot_binding=binding)


@pytest.mark.parametrize(
    ("verified", "reference", "authorized"),
    [
        pytest.param(True, "", True, id="cifrado-verificado-como-johanna"),
        pytest.param(False, "", False, id="sin-cifrar-y-sin-aceptacion"),
        pytest.param(False, ATT1_RISK_ACCEPTANCE_REF, True, id="sin-cifrar-con-aceptacion-https"),
        pytest.param(False, "http://example.test/aceptacion.md", False, id="aceptacion-por-http"),
        pytest.param(
            False, "https://dan:clave@example.test/aceptacion.md", False,
            id="aceptacion-con-credenciales",
        ),
        pytest.param(False, "https://dan@example.test/aceptacion.md", False, id="aceptacion-con-usuario"),
        pytest.param(False, "   ", False, id="aceptacion-en-blanco"),
        pytest.param(False, None, False, id="aceptacion-que-no-es-texto"),
        pytest.param(True, ATT1_RISK_ACCEPTANCE_REF, False, id="cifrado-y-aceptacion-a-la-vez"),
    ],
)
def test_collector_accepts_unencrypted_storage_only_with_a_risk_acceptance_ref(
    verified: bool, reference: object, authorized: bool
) -> None:
    policy = _att1_security_policy(
        storage_encryption_verified=verified,
        storage_risk_acceptance_ref=reference,
    )
    if not authorized:
        with pytest.raises(
            ConversationCollectionError,
            match="real_conversation_collection_not_authorized",
        ):
            _att1_collector(_chatwoot({}, []), security_policy=policy)
        return

    collector = _att1_collector(_chatwoot({}, []), security_policy=policy)

    assert collector.security_policy == policy


def test_unencrypted_storage_is_never_declared_encrypted_in_the_package() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/accounts/2/conversations"
        return httpx.Response(200, json={"data": {"meta": {"all_count": 0}, "payload": []}})

    package = _att1_collector(httpx.MockTransport(handler)).collect(
        tenant_ref="lancemos",
        scope_ref="att1-agent-bot",
        window_start=datetime(2026, 10, 9, tzinfo=UTC),
        window_end=datetime(2026, 10, 10, tzinfo=UTC),
    )

    assert package.storage_encryption_verified is False
    assert package.conversations == ()


@pytest.mark.parametrize(
    "reference",
    [
        "",
        "http://example.test/aceptacion.md",
        "ftp://example.test/aceptacion.md",
        "https:///aceptacion.md",
        "https://dan:clave@example.test/aceptacion.md",
        "https://@example.test/aceptacion.md",
        " https://example.test/aceptacion.md",
        "https://example.test/acepta cion.md",
        "https://example.test/aceptacion.md\n",
        # Caracteres de control que no son espacios: urlsplit borra el del principio
        # en silencio y deja pasar el del medio, asi que los frena isprintable.
        "\x01https://example.test/aceptacion.md",
        "https://example.test/acepta\x1bcion.md",
        "https://example.test:99999/aceptacion.md",
        # urlsplit levanta su propio error con estos: sale igual el codigo de la regla
        "https://[x]/aceptacion.md",
        "https://[::1/aceptacion.md",
        None,
        b"https://example.test/aceptacion.md",
    ],
)
def test_storage_risk_acceptance_ref_is_https_without_credentials(
    reference: object,
) -> None:
    assert (
        validate_storage_risk_acceptance_ref(ATT1_RISK_ACCEPTANCE_REF)
        == ATT1_RISK_ACCEPTANCE_REF
    )
    with pytest.raises(ValueError, match="invalid_storage_risk_acceptance_ref"):
        validate_storage_risk_acceptance_ref(reference)


@pytest.mark.parametrize("pages", [0, -1, 401, True, 2.0, "20", None])
def test_max_conversation_pages_accepts_one_to_four_hundred(pages: object) -> None:
    assert validate_max_conversation_pages(1) == 1
    assert validate_max_conversation_pages(400) == 400
    with pytest.raises(ValueError, match="invalid_daily_feedback_max_conversation_pages"):
        validate_max_conversation_pages(pages)
    with pytest.raises(ValueError, match="invalid_daily_feedback_max_conversation_pages"):
        _att1_collector(_chatwoot({}, []), max_conversation_pages=pages)


def test_max_conversation_pages_bounds_the_conversation_listing() -> None:
    # 60 conversaciones de a 25 por pagina: hacen falta 3. Ninguna tuvo actividad en
    # la ventana, asi que no se pide ningun mensaje.
    requested_pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/accounts/2/conversations"
        page = int(request.url.params["page"])
        requested_pages.append(page)
        first = (page - 1) * 25 + 1
        return httpx.Response(
            200,
            json={
                "data": {
                    "meta": {"all_count": 60},
                    "payload": [
                        {
                            "id": conversation_id,
                            "inbox_id": 11,
                            "updated_at": datetime(2026, 10, 1, tzinfo=UTC).timestamp(),
                        }
                        for conversation_id in range(first, min(first + 25, 61))
                    ],
                }
            },
        )

    window = {
        "tenant_ref": "lancemos",
        "scope_ref": "att1-agent-bot",
        "window_start": datetime(2026, 10, 9, tzinfo=UTC),
        "window_end": datetime(2026, 10, 10, tzinfo=UTC),
    }
    assert DEFAULT_MAX_CONVERSATION_PAGES == 20
    assert _att1_collector(_chatwoot({}, [])).max_conversation_pages == 20

    with pytest.raises(
        ConversationCollectionError, match="chatwoot_conversation_page_limit_reached"
    ):
        _att1_collector(
            httpx.MockTransport(handler), max_conversation_pages=2
        ).collect(**window)
    assert requested_pages == [1, 2]

    requested_pages.clear()
    collector = _att1_collector(httpx.MockTransport(handler), max_conversation_pages=3)
    assert collector.max_conversation_pages == 3
    assert collector.collect(**window).conversations == ()
    assert requested_pages == [1, 2, 3]


def test_classifies_att1_unassignment_activities() -> None:
    capture = _capture("chatwoot_audio_trigger_inbox_11_conv_1_20261006.json")
    messages = {message["id"]: message for message in capture["messages"]["payload"]}

    def classified(message: dict[str, object]) -> tuple[str, str, dict[str, object]]:
        result = classify_chatwoot_message(message, agent_bot_id=2)
        assert result is not None
        return (result.actor, result.kind, result.meta)

    assert messages[2679]["content"] == "Conversation unassigned by AGENTE"
    assert classified(messages[2679]) == ("system", "unassigned", {"actor_name": "AGENTE"})
    assert messages[2680]["content"] == "Unassigned from att1 - revisión humana by AGENTE"
    assert classified(messages[2680]) == (
        "system",
        "unassigned",
        {"team": "att1 - revisión humana", "actor_name": "AGENTE"},
    )
    # Ninguna actividad capturada de ATT1 cae en el generico.
    assert "activity" not in {
        classified(message)[1]
        for message in messages.values()
        if message["message_type"] == 2
    }
    # La forma que ya se reconocia sigue igual, y quien lo hizo es lo que sigue al
    # ultimo " by " aunque el equipo lo lleve en el nombre.
    assert classified(
        {"message_type": 2, "content": "Unassigned by Bridge Service", "sender": None}
    ) == ("system", "unassigned", {"actor_name": "Bridge Service"})
    assert classified(
        {"message_type": 2, "content": "Unassigned from Ventas by zona by Mariana", "sender": None}
    ) == ("system", "unassigned", {"team": "Ventas by zona", "actor_name": "Mariana"})


def test_v2_collects_a_captured_att1_conversation() -> None:
    # La recoleccion v2 sobre el dato real de ATT1 (inbox 11, conversacion 1, captura
    # del 06/10), con el colector como lo arma la fabrica en ATT1: sin vincular, sin
    # cifrar y con 200 paginas. La ventana es la del corte de las 18:00 de CDMX (UTC-6,
    # medianoche UTC) que cubre los mensajes capturados, todos del 05/10 en UTC.
    from bridge.daily_feedback_service import _package_items

    capture = _capture("chatwoot_audio_trigger_inbox_11_conv_1_20261006.json")
    conversation = capture["conversation"]
    messages = capture["messages"]["payload"]
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        assert request.headers["api_access_token"] == "not-a-real-token"
        if request.url.path == "/api/v1/accounts/2/conversations":
            assert request.url.params["inbox_id"] == "11"
            assert request.url.params["status"] == "all"
            return httpx.Response(
                200,
                json={"data": {"meta": {"all_count": 1}, "payload": [conversation]}},
            )
        if request.url.path == "/api/v1/accounts/2/conversations/1/messages":
            if request.url.params.get("before") is not None:
                return httpx.Response(200, json={"payload": []})
            return httpx.Response(200, json={"payload": messages})
        raise AssertionError(f"unexpected request: {request.url}")

    package = _att1_collector(
        httpx.MockTransport(handler), max_conversation_pages=200
    ).collect(
        tenant_ref="lancemos",
        scope_ref="att1-agent-bot",
        window_start=datetime(2026, 10, 5, tzinfo=UTC),
        window_end=datetime(2026, 10, 6, tzinfo=UTC),
    )

    assert package.schema_version == PACKAGE_SCHEMA_V2
    assert package.storage_encryption_verified is False
    assert len(package.conversations) == 1
    collected = package.conversations[0]
    expected_kinds = [
        ("agent", "agent_reply"),
        ("prospect", "prospect_message"),
        ("system", "automation_paused"),
        ("system", "assigned"),
        ("team", "handoff_note"),
        ("team", "team_message"),
        ("system", "unassigned"),
        ("system", "unassigned"),
        ("system", "automation_resumed"),
        ("prospect", "prospect_message"),
    ]
    assert [(message.actor, message.kind) for message in collected.messages] == expected_kinds
    # La respuesta es del bot 2, el de ATT1, y los audios del lead salen sin texto.
    assert collected.messages[0].meta == {"chatwoot_message_id": 2668}
    assert collected.messages[1].meta == {"empty": True}
    assert collected.messages[7].meta == {
        "team": "att1 - revisión humana",
        "actor_name": "AGENTE",
    }
    assert collected.context["chatwoot_conversation_id"] == 1
    assert (
        collected.context["conversation_url"]
        == "https://chatwoot.example.test/app/accounts/2/conversations/1"
    )

    # Lo que hace el scheduler despues: el contexto de la base (aca, sin filas) y el
    # empaquetado que va al commit, con el claim del lote de ATT1.
    items, lineage = _package_items(
        apply_conversation_context(package, {}),
        {
            "tenant_ref": "lancemos",
            "scope_ref": "att1-agent-bot",
            "window_start": "2026-10-05T00:00:00Z",
            "window_end": "2026-10-06T00:00:00Z",
            "sanitizer_version": SANITIZER_VERSION_V2,
            "selection_version": SELECTION_VERSION_V2,
        },
    )

    assert lineage == "release_lineage_unavailable"
    assert len(items) == 1
    assert [(row["actor"], row["kind"]) for row in items[0]["messages"]] == expected_kinds
    assert items[0]["context"]["chatwoot_conversation_id"] == 1
    assert json.dumps(items, ensure_ascii=False)  # serializable tal cual va a la base
    assert requests == [
        "/api/v1/accounts/2/conversations",
        "/api/v1/accounts/2/conversations/1/messages",
        "/api/v1/accounts/2/conversations/1/messages",
    ]
