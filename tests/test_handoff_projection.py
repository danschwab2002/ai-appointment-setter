import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

from bridge.app import Settings, create_app
from bridge.chatwoot import (
    ChatwootAssignmentConflictError,
    ChatwootClient,
    ChatwootHandoffConflictError,
    ChatwootProtocolError,
)
from bridge.supabase import (
    HumanHandoffProjectionClaim,
    HumanHandoffProjectionFinalization,
    SupabaseClient,
    SupabaseCommittedResponseError,
    SupabaseError,
)
from bridge.worker import HumanHandoffProjectionWorker


ALLOWED_JID = "12025550123@s.whatsapp.net"


def test_supabase_claims_typed_handoff_projection_effects() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=[
                {
                    "effect_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee1",
                    "handoff_request_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1",
                    "effect_kind": "private_note",
                    "current_effect_status": "delivery_unknown",
                    "attempt_count": 2,
                    "lease_generation": 3,
                    "expected_team_id": 17,
                    "chatwoot_account_id": 1,
                    "chatwoot_inbox_id": 7,
                    "chatwoot_conversation_id": 42,
                    "private_note_body": "Revisá la conversación.",
                    "idempotency_marker": "[supportmagician-handoff:test:v1]",
                }
            ],
        )

    client = SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="service-key",
        transport=httpx.MockTransport(handler),
    )
    claims = asyncio.run(
        client.claim_human_handoff_projection_effects(
            worker_id="handoff-worker",
            now="2026-08-10T00:00:00+00:00",
            lease_seconds=60,
            batch_size=10,
        )
    )

    assert len(claims) == 1
    assert claims[0].effect_kind == "private_note"
    assert claims[0].current_effect_status == "delivery_unknown"
    assert claims[0].attempt_count == 2
    assert claims[0].chatwoot_conversation_id == 42
    assert claims[0].expected_team_id == 17
    assert requests[0].url.path.endswith(
        "/rest/v1/rpc/claim_human_handoff_projection_effects"
    )
    assert json.loads(requests[0].content) == {
        "p_worker_id": "handoff-worker",
        "p_now": "2026-08-10T00:00:00+00:00",
        "p_lease_seconds": 60,
        "p_limit": 10,
    }


def test_supabase_finalizes_handoff_projection_effect() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=[
                {
                    "effect_status": "retryable_failed",
                    "handoff_status": "projection_failed",
                }
            ],
        )

    client = SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="service-key",
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(
        client.finalize_human_handoff_projection_effect(
            effect_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee1",
            worker_id="handoff-worker",
            lease_generation=3,
            outcome="retryable_failed",
            error_code="chatwoot_timeout",
            retry_at="2026-08-10T00:01:00+00:00",
            now="2026-08-10T00:00:01+00:00",
        )
    )

    assert result.effect_status == "retryable_failed"
    assert result.handoff_status == "projection_failed"
    assert requests[0].url.path.endswith(
        "/rest/v1/rpc/finalize_human_handoff_projection_effect"
    )
    assert json.loads(requests[0].content) == {
        "p_effect_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee1",
        "p_worker_id": "handoff-worker",
        "p_lease_generation": 3,
        "p_outcome": "retryable_failed",
        "p_error_code": "chatwoot_timeout",
        "p_retry_at": "2026-08-10T00:01:00+00:00",
        "p_now": "2026-08-10T00:00:01+00:00",
    }


def test_supabase_rejects_mismatched_committed_handoff_finalization() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json=[{
            "effect_status": "applied",
            "handoff_status": "projected",
        }])
    )
    client = SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="service-key",
        transport=transport,
    )
    with pytest.raises(
        SupabaseCommittedResponseError,
        match="committed_response_mismatch",
    ):
        asyncio.run(
            client.finalize_human_handoff_projection_effect(
                effect_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee1",
                worker_id="handoff-worker",
                lease_generation=3,
                outcome="retryable_failed",
                error_code="chatwoot_timeout",
                retry_at="2026-08-10T00:01:00+00:00",
                now="2026-08-10T00:00:01+00:00",
            )
        )


def test_supabase_rejects_negative_handoff_readiness_count() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json=[{
            "pending_count": -1,
            "retryable_count": 0,
            "delivery_unknown_count": 0,
            "conflict_count": 0,
            "dead_letter_count": 0,
        }])
    )
    client = SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="service-key",
        transport=transport,
    )
    with pytest.raises(SupabaseError, match="invalid_row"):
        asyncio.run(client.get_human_handoff_projection_status())


def _conversation(
    *,
    assignee: object = None,
    assignee_type: object = "User",
    team: object = None,
) -> dict[str, object]:
    return {
        "id": 42,
        "inbox_id": 7,
        "meta": {
            "sender": {"identifier": ALLOWED_JID},
            "assignee": assignee,
            "assignee_type": assignee_type,
            "team": team,
        },
    }


def _chatwoot_client(handler: httpx.MockTransport) -> ChatwootClient:
    return ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=1,
        access_token="control-token",
        allowed_jid=ALLOWED_JID,
        transport=handler,
    )


def test_handoff_assignment_respects_an_existing_human() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_conversation(assignee={"id": 9}))

    outcome = asyncio.run(
        _chatwoot_client(httpx.MockTransport(handler)).ensure_handoff_assignment(
            conversation_id=42,
            expected_inbox_id=7,
            expected_team_id=17,
        )
    )
    assert outcome == "existing_human"
    assert [request.method for request in requests] == ["GET"]


def test_handoff_assignment_replaces_agent_bot_with_expected_team() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"ok": True})
        team = {"id": 17} if len(requests) > 2 else None
        return httpx.Response(200, json=_conversation(
            assignee={"id": 77},
            assignee_type="AgentBot",
            team=team,
        ))

    outcome = asyncio.run(
        _chatwoot_client(httpx.MockTransport(handler)).ensure_handoff_assignment(
            conversation_id=42,
            expected_inbox_id=7,
            expected_team_id=17,
        )
    )
    assert outcome == "team_assigned"
    assert [request.method for request in requests] == ["GET", "POST", "GET"]


def test_handoff_assignment_rejects_unknown_assignee_type() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(
        200,
        json=_conversation(assignee={"id": 77}, assignee_type="Unknown"),
    ))
    with pytest.raises(ChatwootProtocolError, match="invalid_handoff_assignee_type"):
        asyncio.run(
            _chatwoot_client(transport).ensure_handoff_assignment(
                conversation_id=42,
                expected_inbox_id=7,
                expected_team_id=17,
            )
        )


def test_handoff_assignment_does_not_overwrite_another_team() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_conversation(team={"id": 99}))

    with pytest.raises(ChatwootAssignmentConflictError, match="unexpected_team"):
        asyncio.run(
            _chatwoot_client(
                httpx.MockTransport(handler)
            ).ensure_handoff_assignment(
                conversation_id=42,
                expected_inbox_id=7,
                expected_team_id=17,
            )
        )
    assert [request.method for request in requests] == ["GET"]


def test_handoff_assignment_posts_team_and_confirms() -> None:
    requests: list[httpx.Request] = []
    get_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_count
        requests.append(request)
        if request.method == "GET":
            get_count += 1
            team = None if get_count == 1 else {"id": 17}
            return httpx.Response(200, json=_conversation(team=team))
        assert request.method == "POST"
        assert request.url.path.endswith("/conversations/42/assignments")
        assert json.loads(request.content) == {"team_id": 17}
        return httpx.Response(200, json={"team": {"id": 17}})

    outcome = asyncio.run(
        _chatwoot_client(httpx.MockTransport(handler)).ensure_handoff_assignment(
            conversation_id=42,
            expected_inbox_id=7,
            expected_team_id=17,
        )
    )
    assert outcome == "team_assigned"
    assert [request.method for request in requests] == ["GET", "POST", "GET"]


def test_private_handoff_note_is_idempotent_by_stable_marker() -> None:
    marker = "[supportmagician-handoff:test:v1]"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/conversations/42"):
            return httpx.Response(200, json=_conversation())
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={
                "payload": [
                    {
                        "id": 101,
                        "private": True,
                        "message_type": 1,
                        "content": f"Nota previa\n\n{marker}",
                    }
                ]
            },
        )

    applied = asyncio.run(
        _chatwoot_client(httpx.MockTransport(handler)).ensure_private_handoff_note(
            conversation_id=42,
            expected_inbox_id=7,
            note_body="Revisá la conversación.",
            idempotency_marker=marker,
            create_if_missing=True,
        )
    )
    assert applied is True
    assert [request.method for request in requests] == ["GET", "GET"]


def test_delivery_unknown_private_note_scans_without_posting_again() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/conversations/42"):
            return httpx.Response(200, json=_conversation())
        return httpx.Response(200, json={"payload": []})

    applied = asyncio.run(
        _chatwoot_client(httpx.MockTransport(handler)).ensure_private_handoff_note(
            conversation_id=42,
            expected_inbox_id=7,
            note_body="Revisá la conversación.",
            idempotency_marker="[supportmagician-handoff:test:v1]",
            create_if_missing=False,
        )
    )
    assert applied is False
    assert [request.method for request in requests] == ["GET", "GET"]


def test_private_handoff_note_posts_once_when_marker_is_absent() -> None:
    marker = "[supportmagician-handoff:test:v1]"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/conversations/42"):
            return httpx.Response(200, json=_conversation())
        if request.method == "GET":
            return httpx.Response(200, json={"payload": []})
        content = f"Revisá la conversación.\n\n{marker}"
        return httpx.Response(200, json={
            "id": 102,
            "conversation_id": 42,
            "private": True,
            "message_type": 1,
            "content": content,
        })

    applied = asyncio.run(
        _chatwoot_client(httpx.MockTransport(handler)).ensure_private_handoff_note(
            conversation_id=42,
            expected_inbox_id=7,
            note_body="Revisá la conversación.",
            idempotency_marker=marker,
            create_if_missing=True,
        )
    )
    assert applied is True
    assert [request.method for request in requests] == ["GET", "GET", "POST"]


def test_private_handoff_note_rejects_duplicate_markers() -> None:
    marker = "[supportmagician-handoff:test:v1]"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/conversations/42"):
            return httpx.Response(200, json=_conversation())
        return httpx.Response(200, json={"payload": [
            {"id": 101, "private": True, "message_type": 1, "content": marker},
            {"id": 102, "private": True, "message_type": 1, "content": marker},
        ]})

    with pytest.raises(ChatwootProtocolError, match="duplicate_handoff"):
        asyncio.run(
            _chatwoot_client(
                httpx.MockTransport(handler)
            ).ensure_private_handoff_note(
                conversation_id=42,
                expected_inbox_id=7,
                note_body="Revisá la conversación.",
                idempotency_marker=marker,
                create_if_missing=True,
            )
        )


def _claim(
    *, effect_kind: str, current_status: str = "pending", attempt_count: int = 1
) -> HumanHandoffProjectionClaim:
    return HumanHandoffProjectionClaim(
        effect_id=f"effect-{effect_kind}",
        handoff_request_id="handoff-1",
        effect_kind=effect_kind,
        current_effect_status=current_status,
        attempt_count=attempt_count,
        lease_generation=2,
        expected_team_id=17,
        chatwoot_account_id=1,
        chatwoot_inbox_id=7,
        chatwoot_conversation_id=42,
        private_note_body="Revisá la conversación.",
        idempotency_marker="[supportmagician-handoff:test:v1]",
    )


class _FakeHandoffSupabase:
    def __init__(self, claims: list[HumanHandoffProjectionClaim]) -> None:
        self.claims = claims
        self.finalizations: list[dict[str, object]] = []

    async def claim_human_handoff_projection_effects(
        self, **_: object
    ) -> list[HumanHandoffProjectionClaim]:
        return self.claims

    async def finalize_human_handoff_projection_effect(
        self, **kwargs: object
    ) -> HumanHandoffProjectionFinalization:
        self.finalizations.append(kwargs)
        return HumanHandoffProjectionFinalization(
            effect_status=str(kwargs["outcome"]),
            handoff_status="projection_failed",
        )


class _FakeHandoffChatwoot:
    account_id = 1

    def __init__(self, *, note_result: bool = True) -> None:
        self.note_result = note_result
        self.assignments: list[dict[str, object]] = []
        self.notes: list[dict[str, object]] = []

    async def ensure_handoff_assignment(self, **kwargs: object) -> str:
        self.assignments.append(kwargs)
        return "team_assigned"

    async def ensure_private_handoff_note(self, **kwargs: object) -> bool:
        self.notes.append(kwargs)
        return self.note_result


def test_handoff_worker_projects_both_effects_and_finalizes_them() -> None:
    supabase = _FakeHandoffSupabase(
        [_claim(effect_kind="assignment"), _claim(effect_kind="private_note")]
    )
    chatwoot = _FakeHandoffChatwoot()
    worker = HumanHandoffProjectionWorker(
        supabase=supabase,  # type: ignore[arg-type]
        chatwoot=chatwoot,  # type: ignore[arg-type]
        worker_id="handoff-worker",
        clock=lambda: "2026-08-10T00:00:00+00:00",
    )

    assert asyncio.run(worker.run_once()) == 2
    assert len(chatwoot.assignments) == 1
    assert len(chatwoot.notes) == 1
    assert chatwoot.notes[0]["create_if_missing"] is True
    assert [item["outcome"] for item in supabase.finalizations] == [
        "applied",
        "applied",
    ]


def test_handoff_worker_never_reposts_a_delivery_unknown_note() -> None:
    supabase = _FakeHandoffSupabase(
        [
            _claim(
                effect_kind="private_note",
                current_status="delivery_unknown",
                attempt_count=2,
            )
        ]
    )
    chatwoot = _FakeHandoffChatwoot(note_result=False)
    worker = HumanHandoffProjectionWorker(
        supabase=supabase,  # type: ignore[arg-type]
        chatwoot=chatwoot,  # type: ignore[arg-type]
        worker_id="handoff-worker",
        clock=lambda: "2026-08-10T00:00:00+00:00",
    )

    assert asyncio.run(worker.run_once()) == 1
    assert chatwoot.notes[0]["create_if_missing"] is False
    assert supabase.finalizations[0]["outcome"] == "delivery_unknown"
    assert supabase.finalizations[0]["retry_at"] is not None


def test_handoff_worker_dead_letters_unresolved_unknown_at_attempt_cap() -> None:
    supabase = _FakeHandoffSupabase([
        _claim(
            effect_kind="private_note",
            current_status="delivery_unknown",
            attempt_count=2,
        )
    ])
    chatwoot = _FakeHandoffChatwoot(note_result=False)
    worker = HumanHandoffProjectionWorker(
        supabase=supabase,  # type: ignore[arg-type]
        chatwoot=chatwoot,  # type: ignore[arg-type]
        worker_id="handoff-worker",
        max_attempts=2,
        clock=lambda: "2026-08-10T00:00:00+00:00",
    )

    assert asyncio.run(worker.run_once()) == 1
    assert chatwoot.notes[0]["create_if_missing"] is False
    assert supabase.finalizations[0]["outcome"] == "dead_letter"
    assert supabase.finalizations[0]["retry_at"] is None


def test_handoff_worker_finalizes_duplicate_note_marker_as_conflict() -> None:
    supabase = _FakeHandoffSupabase([_claim(effect_kind="private_note")])

    class ConflictingChatwoot(_FakeHandoffChatwoot):
        async def ensure_private_handoff_note(self, **_: object) -> bool:
            raise ChatwootHandoffConflictError(
                "duplicate_handoff_private_note_marker"
            )

    worker = HumanHandoffProjectionWorker(
        supabase=supabase,  # type: ignore[arg-type]
        chatwoot=ConflictingChatwoot(),  # type: ignore[arg-type]
        worker_id="handoff-worker",
        clock=lambda: "2026-08-10T00:00:00+00:00",
    )

    assert asyncio.run(worker.run_once()) == 1
    assert supabase.finalizations[0]["outcome"] == "conflict"
    assert supabase.finalizations[0]["error_code"] == (
        "duplicate_handoff_private_note_marker"
    )


def test_handoff_worker_stop_is_bounded_during_stuck_finalization() -> None:
    async def scenario() -> tuple[float, bool]:
        entered = asyncio.Event()
        release = asyncio.Event()
        cancellation_suppressed = asyncio.Event()

        class SlowSupabase(_FakeHandoffSupabase):
            async def finalize_human_handoff_projection_effect(
                self, **_: object
            ) -> HumanHandoffProjectionFinalization:
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancellation_suppressed.set()
                    await release.wait()
                raise AssertionError("unreachable")

        supabase = SlowSupabase([_claim(effect_kind="assignment")])
        worker = HumanHandoffProjectionWorker(
            supabase=supabase,  # type: ignore[arg-type]
            chatwoot=_FakeHandoffChatwoot(),  # type: ignore[arg-type]
            worker_id="handoff-worker",
            poll_interval_seconds=60,
            finalization_timeout_seconds=0.01,
            clock=lambda: "2026-08-10T00:00:00+00:00",
        )
        await worker.start()
        await asyncio.wait_for(entered.wait(), timeout=1)
        started = time.monotonic()
        await worker.stop(timeout=0.01)
        elapsed = time.monotonic() - started
        release.set()
        await asyncio.sleep(0)
        return elapsed, cancellation_suppressed.is_set()

    elapsed, cancellation_was_suppressed = asyncio.run(scenario())
    assert elapsed < 0.1
    assert cancellation_was_suppressed is True


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "webhook_secret": "webhook-secret",
        "allowed_jid": ALLOWED_JID,
        "capture_dir": tmp_path,
        "max_age_seconds": 300,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_handoff_readiness_reports_only_sanitized_projection_counts(
    tmp_path: Path,
) -> None:
    def supabase_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/get_human_handoff_projection_status")
        return httpx.Response(200, json=[{
            "pending_count": 2,
            "retryable_count": 1,
            "delivery_unknown_count": 1,
            "conflict_count": 0,
            "dead_letter_count": 0,
        }])

    supabase = SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="service-role-key",
        transport=httpx.MockTransport(supabase_handler),
    )
    app = create_app(
        _settings(
            tmp_path,
            human_handoff_projection_enabled=True,
            human_handoff_projection_worker_id="handoff-worker",
            chatwoot_account_id=1,
            chatwoot_inbox_id=7,
        ),
        supabase_client=supabase,
        chatwoot_client=_chatwoot_client(
            httpx.MockTransport(lambda _: httpx.Response(500))
        ),
    )
    ready_route = next(
        route for route in app.routes if getattr(route, "path", None) == "/ready"
    )
    response = asyncio.run(getattr(ready_route, "endpoint")())
    assert response == {
        "status": "ready",
        "pilot_boundary": "disabled",
        "automation_state": "default_off",
        "reason_code": "pilot_boundary_disabled",
        "human_handoff_projection": "configured",
        "human_handoff_pending": "2",
        "human_handoff_retryable": "1",
        "human_handoff_delivery_unknown": "1",
        "human_handoff_conflicts": "0",
        "human_handoff_dead_letters": "0",
    }


def test_handoff_projection_worker_is_default_off(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    assert app.state.human_handoff_projection_worker is None


def test_enabled_handoff_projection_requires_durable_dependencies(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        ValueError,
        match="HUMAN_HANDOFF_PROJECTION_ENABLED requires Supabase and Chatwoot control",
    ):
        create_app(
            _settings(
                tmp_path,
                human_handoff_projection_enabled=True,
                human_handoff_projection_worker_id="handoff-worker",
                chatwoot_account_id=1,
                chatwoot_inbox_id=7,
            )
        )
