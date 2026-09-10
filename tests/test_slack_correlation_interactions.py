from __future__ import annotations

import hashlib
import hmac
import json
import asyncio
import time
from datetime import UTC, datetime
from urllib.parse import urlencode

from fastapi.testclient import TestClient
import pytest

from slack_correlation.app import SlackConnectorSettings, create_app
from slack_correlation.catalog import NotificationCommand, render_message
from slack_correlation.store import NotificationStore
from slack_correlation.ui import build_pending_message

TEAM = "T12345678"
CHANNEL = "C0C0YEACVT2"
USER = "U12345678"
CASE = "11111111-1111-4111-8111-111111111111"
TS = "1789000000.000001"
SECRET = "synthetic-signing-secret"


class FakeSlack:
    def __init__(self) -> None:
        self.views: list[tuple[str, dict]] = []
        self.updates: list[tuple[str, str, dict]] = []
        self.view_updates: list[tuple[str, str | None, dict]] = []

    async def verify_auth(self, *, expected_team_id: str) -> None:
        assert expected_team_id == TEAM

    async def open_view(self, *, trigger_id: str, view: dict, expected_team_id: str) -> None:
        assert expected_team_id == TEAM
        self.views.append((trigger_id, view))

    async def update_message(self, *, channel_id: str, message_ts: str, message: dict):
        self.updates.append((channel_id, message_ts, message))

    async def update_view(self, *, view_id: str, view_hash: str | None, view: dict,
                          expected_team_id: str) -> None:
        assert expected_team_id == TEAM
        self.view_updates.append((view_id, view_hash, view))


class FakeOperator:
    def __init__(self) -> None:
        self.get_calls = 0
        self.prepare_calls: list[dict] = []
        self.confirm_calls: list[dict] = []

    async def get_case(self, case_id: str) -> dict:
        self.get_calls += 1
        assert case_id == CASE
        return {
            "case_id": CASE,
            "outcome": "unmatched",
            "candidate_count": 1,
            "automation_blocked": True,
            "identity": {"masked_email": "a***z@example.com", "masked_phone": "********4567"},
            "candidates": [{
                "purchase_intent_id": "22222222-2222-4222-8222-222222222222",
                "lifecycle_state": "waiting_for_purchase",
                "masked_email": "a***z@example.com",
                "masked_phone": "********4567",
            }],
        }

    async def prepare(self, **kwargs) -> dict:
        self.prepare_calls.append(kwargs)
        return {"command_id": "33333333-3333-4333-8333-333333333333", **kwargs}

    async def confirm(self, **kwargs) -> dict:
        self.confirm_calls.append(kwargs)
        return {
            "resolution_id": "44444444-4444-4444-8444-444444444444",
            "case_id": CASE,
            "resolution_outcome": "linked_candidate",
            "effective_purchase_intent_id": "22222222-2222-4222-8222-222222222222",
            "applied_at": "2026-09-09T12:10:00Z",
            "automation_blocked": True,
        }


class SlowPrepareOperator(FakeOperator):
    async def prepare(self, **kwargs) -> dict:
        self.prepare_calls.append(kwargs)
        await asyncio.sleep(3)
        return {"command_id": "33333333-3333-4333-8333-333333333333", **kwargs}


class AmbiguousConfirmOperator(FakeOperator):
    async def confirm(self, **kwargs) -> dict:
        self.confirm_calls.append(kwargs)
        if len(self.confirm_calls) == 1:
            raise RuntimeError("ambiguous_remote_success")
        return {
            "resolution_id": "44444444-4444-4444-8444-444444444444",
            "case_id": CASE,
            "resolution_outcome": "linked_candidate",
            "effective_purchase_intent_id": "22222222-2222-4222-8222-222222222222",
            "applied_at": "2026-09-09T12:10:00Z",
            "automation_blocked": True,
        }


class CancellationSuppressingOperator(FakeOperator):
    def __init__(self, blocked_method: str) -> None:
        super().__init__()
        self.blocked_method = blocked_method
        self.release = asyncio.Event()

    async def _block(self) -> None:
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                continue

    async def get_case(self, case_id: str) -> dict:
        if self.blocked_method == "get_case":
            await self._block()
        return await super().get_case(case_id)

    async def prepare(self, **kwargs) -> dict:
        self.prepare_calls.append(kwargs)
        if self.blocked_method == "prepare":
            await self._block()
        return {"command_id": "33333333-3333-4333-8333-333333333333", **kwargs}


class CancellationSuppressingSlack(FakeSlack):
    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def open_view(self, *, trigger_id: str, view: dict, expected_team_id: str) -> None:
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                continue
        await super().open_view(
            trigger_id=trigger_id, view=view, expected_team_id=expected_team_id
        )


class CancellationSuppressingUpdateSlack(FakeSlack):
    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def update_view(self, *, view_id: str, view_hash: str | None, view: dict,
                          expected_team_id: str) -> None:
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                continue
        await super().update_view(
            view_id=view_id, view_hash=view_hash, view=view,
            expected_team_id=expected_team_id,
        )


def _accepted_correlation(store: NotificationStore) -> None:
    store.initialize()
    command = NotificationCommand(
        event_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        event_code="COR-001",
        dedupe_key="a" * 64,
        occurred_at=datetime(2026, 9, 9, 12, tzinfo=UTC),
        subject_ref=f"C-{CASE}",
    )
    store.admit(tenant_ref="johanna", command=command, channel_id=CHANNEL)
    claim = store.claim_next(worker_id="worker-1")
    assert claim is not None
    store.mark_request_started(claim)
    store.finalize_accepted(claim, channel_id=CHANNEL, message_ts=TS, thread_ts=None, team_id=TEAM)


def _signed(client: TestClient, payload: dict, *, timestamp: str = "1789000000"):
    body = urlencode({"payload": json.dumps(payload, separators=(",", ":"))}).encode()
    signature = "v0=" + hmac.new(
        SECRET.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256
    ).hexdigest()
    return client.post(
        "/slack/interactions",
        content=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": timestamp,
            "X-Slack-Signature": signature,
        },
    )


def _wait_for_open_view(slack: FakeSlack) -> str:
    deadline = time.monotonic() + 2
    while not slack.views and time.monotonic() < deadline:
        time.sleep(0.02)
    assert slack.views
    return json.loads(slack.views[0][1]["private_metadata"])["review_token"]


def _app(tmp_path, *, allowed=frozenset({USER}), operator=None):
    store = NotificationStore(tmp_path / "connector.sqlite3")
    _accepted_correlation(store)
    slack = FakeSlack()
    operator = operator or FakeOperator()
    app = create_app(
        SlackConnectorSettings(
            interactions_enabled=True,
            bot_token="xoxb-synthetic",
            signing_secret=SECRET,
            team_id=TEAM,
            tenant_channels={"johanna": CHANNEL},
            tenant_operator_user_ids={"johanna": allowed},
            operator_backends={"johanna": {"base_url": "https://operator.example", "read_token": "r" * 32, "write_token": "w" * 32}},
            storage_path=str(tmp_path / "connector.sqlite3"),
        ),
        store=store,
        slack_client=slack,
        operator_clients={"johanna": operator},
        epoch_clock=lambda: 1789000000,
    )
    return app, store, slack, operator


def test_interactions_route_is_absent_by_default() -> None:
    with TestClient(create_app(SlackConnectorSettings())) as client:
        assert client.post("/slack/interactions").status_code == 404


def test_restart_reclaims_interaction_left_request_started(tmp_path) -> None:
    store = NotificationStore(tmp_path / "connector.sqlite3")
    store.initialize()
    fingerprint = "a" * 64
    assert store.reserve_interaction(fingerprint=fingerprint)[0] is True
    store.recover_incomplete()
    assert store.reserve_interaction(fingerprint=fingerprint)[0] is True


def test_prepare_retry_reuses_persisted_idempotency_identity_and_conflicts_fail_closed(tmp_path) -> None:
    store = NotificationStore(tmp_path / "connector.sqlite3")
    _accepted_correlation(store)
    binding = store.find_correlation_binding(
        tenant_ref="johanna", team_id=TEAM, channel_id=CHANNEL, message_ts=TS,
    )
    assert binding is not None
    session = store.create_review_session(
        binding=binding, team_id=TEAM, slack_user_id=USER, expires_at=1789000900,
    )
    first = store.begin_prepare(
        review_token=session.review_token, action="close_without_match", candidate_id=None,
        verification_basis="no_valid_candidate_after_review",
    )
    retry = store.begin_prepare(
        review_token=session.review_token, action="close_without_match", candidate_id=None,
        verification_basis="no_valid_candidate_after_review",
    )
    assert retry.idempotency_key == first.idempotency_key
    with pytest.raises(RuntimeError, match="review_session_semantic_conflict"):
        store.begin_prepare(
            review_token=session.review_token, action="resolve_with_candidate",
            candidate_id="22222222-2222-4222-8222-222222222222",
            verification_basis="customer_confirmation",
        )


def test_block_action_binds_accepted_root_and_opens_native_modal(tmp_path) -> None:
    app, store, slack, operator = _app(tmp_path)
    payload = {
        "type": "block_actions",
        "team": {"id": TEAM},
        "user": {"id": USER},
        "channel": {"id": CHANNEL},
        "message": {"ts": TS, "metadata": {"event_type": "supportmagician_operational_event", "event_payload": {"event_code": "COR-001", "case_id": CASE}}},
        "trigger_id": "123.456.valid",
        "actions": [{"action_id": "review_operator_correlation", "value": CASE}],
    }
    with TestClient(app) as client:
        first = _signed(client, payload)
        replay = _signed(client, payload)
        _wait_for_open_view(slack)

    assert first.status_code == replay.status_code == 200
    assert len(slack.views) == 1
    assert operator.get_calls == 1
    assert slack.views[0][1]["callback_id"] == "prepare_operator_correlation_resolution"
    assert store.interaction_replay_count() == 1


def test_realistic_slack_block_action_extras_are_accepted(tmp_path) -> None:
    app, _store, slack, _operator = _app(tmp_path)
    payload = {
        "type": "block_actions",
        "token": "legacy-verification-token",
        "api_app_id": "A12345678",
        "team": {"id": TEAM, "domain": "example"},
        "enterprise": None,
        "user": {"id": USER, "username": "operator", "name": "operator", "team_id": TEAM},
        "channel": {"id": CHANNEL, "name": "johanna-ops"},
        "container": {"type": "message", "message_ts": TS, "channel_id": CHANNEL, "is_ephemeral": False},
        "message": {"ts": TS, "type": "message", "text": "safe", "user": "U00000000", "team": TEAM, "blocks": [],
                    "client_msg_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    "edited": {"user": "U00000000", "ts": TS}, "reply_count": 0,
                    "reply_users_count": 0, "latest_reply": TS, "reply_users": [],
                    "subscribed": False, "last_read": TS},
        "trigger_id": "123.456.valid",
        "response_url": "https://hooks.slack.com/actions/T/B/opaque",
        "actions": [{
            "action_id": "review_operator_correlation", "value": CASE,
            "type": "button", "block_id": "correlation_actions", "action_ts": TS,
            "text": {"type": "plain_text", "text": "Revisar caso", "emoji": True},
        }],
        "state": {"values": {}},
    }
    with TestClient(app) as client:
        response = _signed(client, payload)
        _wait_for_open_view(slack)
    assert response.status_code == 200
    assert len(slack.views) == 1


def test_unknown_safe_nested_action_field_is_accepted(tmp_path) -> None:
    app, _store, slack, _operator = _app(tmp_path)
    payload = {
        "type": "block_actions", "team": {"id": TEAM}, "user": {"id": USER},
        "channel": {"id": CHANNEL}, "message": {"ts": TS}, "trigger_id": "123.456.valid",
        "actions": [{"action_id": "review_operator_correlation", "value": CASE,
                     "unsafe_unknown": {"raw": "data"}}],
    }
    with TestClient(app) as client:
        response = _signed(client, payload)
        _wait_for_open_view(slack)
    assert response.status_code == 200
    assert len(slack.views) == 1


def test_unauthorized_action_does_not_read_case_or_reveal_details(tmp_path) -> None:
    app, _store, slack, operator = _app(tmp_path, allowed=frozenset({"U99999999"}))
    payload = {
        "type": "block_actions", "team": {"id": TEAM}, "user": {"id": USER},
        "channel": {"id": CHANNEL}, "message": {"ts": TS}, "trigger_id": "123.456.valid",
        "actions": [{"action_id": "review_operator_correlation", "value": CASE}],
    }
    with TestClient(app) as client:
        response = _signed(client, payload)
    assert response.status_code == 200
    assert operator.get_calls == 0
    assert slack.views == []
    assert CASE not in response.text
    assert _interaction_row_counts(tmp_path / "connector.sqlite3") == (0, 0)


def _interaction_row_counts(path) -> tuple[int, int]:
    with __import__("sqlite3").connect(path) as connection:
        return (
            int(connection.execute("SELECT count(*) FROM interaction_replays").fetchone()[0]),
            int(connection.execute("SELECT count(*) FROM correlation_review_sessions").fetchone()[0]),
        )


def test_unauthorized_valid_submission_has_no_local_or_remote_side_effects(tmp_path) -> None:
    app, _store, slack, operator = _app(tmp_path, allowed=frozenset({"U99999999"}))
    payload = {
        "type": "view_submission",
        "team": {"id": TEAM},
        "user": {"id": USER},
        "view": {
            "id": "V12345678",
            "callback_id": "confirm_operator_correlation_resolution",
            "private_metadata": json.dumps(
                {"review_token": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}
            ),
            "state": {"values": {}},
        },
    }
    with TestClient(app) as client:
        response = _signed(client, payload)

    assert response.status_code == 200
    assert response.json() == {}
    assert _interaction_row_counts(tmp_path / "connector.sqlite3") == (0, 0)
    assert operator.get_calls == 0
    assert operator.prepare_calls == []
    assert operator.confirm_calls == []
    assert slack.views == slack.view_updates == slack.updates == []


@pytest.mark.parametrize("blocked", ["get_case", "open_view"])
def test_block_open_ack_does_not_await_cancellation_suppressing_network_client(
    tmp_path, blocked: str,
) -> None:
    operator = CancellationSuppressingOperator(blocked)
    store = NotificationStore(tmp_path / "connector.sqlite3")
    _accepted_correlation(store)
    blocking_slack = CancellationSuppressingSlack()
    slack = blocking_slack if blocked == "open_view" else FakeSlack()
    app = create_app(
        SlackConnectorSettings(
            interactions_enabled=True,
            bot_token="xoxb-synthetic",
            signing_secret=SECRET,
            team_id=TEAM,
            tenant_channels={"johanna": CHANNEL},
            tenant_operator_user_ids={"johanna": frozenset({USER})},
            operator_backends={"johanna": {"base_url": "https://operator.example", "read_token": "r" * 32, "write_token": "w" * 32}},
            storage_path=str(tmp_path / "connector.sqlite3"),
        ),
        store=store,
        slack_client=slack,
        operator_clients={"johanna": operator},
        epoch_clock=lambda: 1789000000,
    )
    payload = {
        "type": "block_actions", "team": {"id": TEAM}, "user": {"id": USER},
        "channel": {"id": CHANNEL}, "message": {"ts": TS},
        "trigger_id": "123.456.valid",
        "actions": [{"action_id": "review_operator_correlation", "value": CASE}],
    }
    with TestClient(app) as client:
        started = time.monotonic()
        response = _signed(client, payload)
        elapsed = time.monotonic() - started
        assert elapsed < 0.75
        assert response.status_code == 200 and response.json() == {}
        assert _interaction_row_counts(tmp_path / "connector.sqlite3") == (1, 1)
        operator.release.set()
        blocking_slack.release.set()


def test_real_slack_bot_message_nested_extras_are_accepted(tmp_path) -> None:
    app, _store, slack, _operator = _app(tmp_path)
    payload = {
        "type": "block_actions", "team": {"id": TEAM, "domain": "example"},
        "user": {"id": USER, "profile": {"display_name": "Operator"}},
        "channel": {"id": CHANNEL, "name": "johanna-ops"},
        "message": {
            "type": "message", "subtype": "bot_message", "ts": TS,
            "text": "safe", "bot_id": "B12345678",
            "bot_profile": {
                "id": "B12345678", "app_id": "A12345678", "name": "connector",
                "icons": {"image_36": "https://cdn.example/36.png",
                          "image_48": "https://cdn.example/48.png"},
                "deleted": False, "updated": 1789000000, "team_id": TEAM,
            },
            "blocks": [{"type": "section", "block_id": "safe",
                        "text": {"type": "mrkdwn", "text": "Review"}}],
        },
        "trigger_id": "123.456.valid",
        "actions": [{"action_id": "review_operator_correlation", "value": CASE,
                     "type": "button", "platform_extension": {"safe": [1, True]}}],
    }
    with TestClient(app) as client:
        response = _signed(client, payload)
        deadline = time.monotonic() + 2
        while not slack.views and time.monotonic() < deadline:
            time.sleep(0.02)
    assert response.status_code == 200
    assert len(slack.views) == 1


@pytest.mark.parametrize("message_kind", ["catalog", "pending"])
def test_exact_connector_rendered_message_round_trips_through_slack_click(
    tmp_path, message_kind: str,
) -> None:
    app, _store, slack, _operator = _app(tmp_path)
    if message_kind == "catalog":
        message = render_message(
            NotificationCommand(
                event_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                event_code="COR-001",
                dedupe_key="a" * 64,
                occurred_at=datetime(2026, 9, 9, 12, tzinfo=UTC),
                subject_ref=f"C-{CASE}",
            ),
            tenant_label="Johanna",
        )
    else:
        message = build_pending_message(
            {
                "case_id": CASE,
                "outcome": "unmatched",
                "candidate_count": 1,
                "automation_blocked": True,
                "identity": {
                    "masked_email": "a***z@example.com",
                    "masked_phone": "********4567",
                },
            },
            review_due_at="2026-09-10T12:00:00Z",
        )
    message.update({"type": "message", "subtype": "bot_message", "ts": TS})
    assert "\\n" in json.dumps(message)
    payload = {
        "type": "block_actions",
        "team": {"id": TEAM},
        "user": {"id": USER},
        "channel": {"id": CHANNEL},
        "message": message,
        "trigger_id": f"123.456.{message_kind}",
        "actions": [{"action_id": "review_operator_correlation", "value": CASE}],
    }
    with TestClient(app) as client:
        response = _signed(client, payload)
        _wait_for_open_view(slack)

    assert response.status_code == 200
    assert len(slack.views) == 1


@pytest.mark.parametrize("unsafe", [
    {"text": "unsafe\u0000control"},
    {"nested": {"a": {"b": {"c": {"d": {"e": {"f": {"g": "deep"}}}}}}}},
    {"items": list(range(101))},
    {"text": "x" * 4097},
])
def test_bot_message_non_authoritative_shapes_fail_closed(tmp_path, unsafe: dict) -> None:
    app, _store, slack, _operator = _app(tmp_path)
    payload = {
        "type": "block_actions", "team": {"id": TEAM}, "user": {"id": USER},
        "channel": {"id": CHANNEL}, "message": {"ts": TS, "bot_profile": unsafe},
        "trigger_id": "123.456.valid",
        "actions": [{"action_id": "review_operator_correlation", "value": CASE}],
    }
    with TestClient(app) as client:
        response = _signed(client, payload)
    assert response.status_code == 400
    assert slack.views == []


def test_prepare_then_confirm_uses_stored_command_and_updates_same_root(tmp_path) -> None:
    app, store, slack, operator = _app(tmp_path)
    open_payload = {
        "type": "block_actions", "team": {"id": TEAM}, "user": {"id": USER},
        "channel": {"id": CHANNEL}, "message": {"ts": TS}, "trigger_id": "123.456.valid",
        "actions": [{"action_id": "review_operator_correlation", "value": CASE}],
    }
    with TestClient(app) as client:
        assert _signed(client, open_payload).status_code == 200
        token = _wait_for_open_view(slack)
        prepare_payload = {
            "type": "view_submission", "token": "legacy-verification-token",
            "api_app_id": "A12345678", "enterprise": None,
            "team": {"id": TEAM, "domain": "example"},
            "user": {"id": USER, "username": "operator", "team_id": TEAM},
            "view": {"id": "V12345678", "team_id": TEAM, "type": "modal",
                     "app_id": "A12345678", "bot_id": "B12345678",
                     "hash": "1789000000.abc", "clear_on_close": False,
                     "notify_on_close": False, "root_view_id": "V12345678",
                     "previous_view_id": None, "external_id": "",
                     "title": {"type": "plain_text", "text": "Revisar correlación"},
                     "submit": {"type": "plain_text", "text": "Preparar"},
                     "close": {"type": "plain_text", "text": "Cancelar"},
                     "blocks": [],
                     "callback_id": "prepare_operator_correlation_resolution", "private_metadata": json.dumps({"review_token": token}), "state": {"values": {
                "resolution": {"selected_resolution": {"selected_option": {"value": "22222222-2222-4222-8222-222222222222"}}},
                "verification": {"verification_basis": {"selected_option": {"value": "customer_confirmation"}}},
            }}},
        }
        prepared = _signed(client, prepare_payload, timestamp="1789000001")
        assert prepared.json()["response_action"] == "update"
        assert prepared.json()["view"]["callback_id"] == "operator_correlation_resolution_processing"
        deadline = time.monotonic() + 2
        while not slack.view_updates and time.monotonic() < deadline:
            time.sleep(0.02)
        assert slack.view_updates[0][2]["callback_id"] == "confirm_operator_correlation_resolution"
        assert operator.confirm_calls == []
        confirm_payload = {
            "type": "view_submission", "team": {"id": TEAM}, "user": {"id": USER},
            "view": {"id": "V12345678", "hash": "1789000001.def", "callback_id": "confirm_operator_correlation_resolution", "private_metadata": json.dumps({"review_token": token}), "state": {"values": {}}},
        }
        confirmed = _signed(client, confirm_payload, timestamp="1789000002")
        deadline = time.monotonic() + 2
        while not slack.updates and time.monotonic() < deadline:
            time.sleep(0.02)
        while len(slack.view_updates) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)

    assert confirmed.status_code == 200
    assert len(operator.prepare_calls) == len(operator.confirm_calls) == 1
    prepared_call = operator.prepare_calls[0]
    assert prepared_call["case_id"] == CASE
    assert prepared_call["action"] == "resolve_with_candidate"
    assert prepared_call["candidate_id"] == "22222222-2222-4222-8222-222222222222"
    assert prepared_call["verification_basis"] == "customer_confirmation"
    assert prepared_call["actor_ref"] == "slack.u12345678"
    assert operator.confirm_calls[0] == {
        "command_id": "33333333-3333-4333-8333-333333333333",
        "expected_action": "resolve_with_candidate",
        "expected_candidate_id": "22222222-2222-4222-8222-222222222222",
        "actor_ref": "slack.u12345678",
    }
    assert slack.updates[0][0:2] == (CHANNEL, TS)
    assert slack.view_updates[-1][2]["callback_id"] == "operator_correlation_resolution_complete"
    assert "actions" not in [block["type"] for block in slack.updates[0][2]["blocks"]]
    assert "slack.u12345678" in repr(slack.updates[0][2])
    assert store.projection_inventory()["delivery_unknown"] == 0
    backup = tmp_path / "interaction-backup.sqlite3"
    store.backup_to(backup)
    restored = tmp_path / "restored.sqlite3"
    NotificationStore.restore_from(backup, restored)
    assert NotificationStore(restored).projection_inventory()["accepted"] == 1


@pytest.mark.parametrize(("payload_extra", "view_extra"), [
    ({}, {"unsafe_unknown": {"raw": "buyer@example.com\n<!channel>"}}),
    ({"response_urls": [{"response_url": "https://hooks.slack.invalid/opaque\n<!channel>"}]}, {}),
])
def test_view_submission_rejects_unknown_unsafe_nested_view_data(
    tmp_path, payload_extra: dict, view_extra: dict,
) -> None:
    app, _store, slack, operator = _app(tmp_path)
    with TestClient(app) as client:
        opened = {
            "type": "block_actions", "team": {"id": TEAM}, "user": {"id": USER},
            "channel": {"id": CHANNEL}, "message": {"ts": TS},
            "trigger_id": "123.456.valid",
            "actions": [{"action_id": "review_operator_correlation", "value": CASE}],
        }
        assert _signed(client, opened).status_code == 200
        token = _wait_for_open_view(slack)
        response = _signed(client, {
            "type": "view_submission", "team": {"id": TEAM}, "user": {"id": USER},
            **payload_extra,
            "view": {
                "callback_id": "prepare_operator_correlation_resolution",
                "private_metadata": json.dumps({"review_token": token}),
                **view_extra,
                "state": {"values": {
                    "resolution": {"selected_resolution": {"selected_option": {"value": "close_without_match"}}},
                    "verification": {"verification_basis": {"selected_option": {"value": "no_valid_candidate_after_review"}}},
                }},
            },
        }, timestamp="1789000001")
    assert response.status_code == 400
    assert operator.prepare_calls == []


def test_prepare_callback_has_hard_deadline_and_remains_retryable(tmp_path) -> None:
    operator = SlowPrepareOperator()
    app, store, slack, _operator = _app(tmp_path, operator=operator)
    open_payload = {
        "type": "block_actions", "team": {"id": TEAM}, "user": {"id": USER},
        "channel": {"id": CHANNEL}, "message": {"ts": TS}, "trigger_id": "123.456.valid",
        "actions": [{"action_id": "review_operator_correlation", "value": CASE}],
    }
    with TestClient(app) as client:
        assert _signed(client, open_payload).status_code == 200
        token = _wait_for_open_view(slack)
        prepare_payload = {
            "type": "view_submission", "team": {"id": TEAM}, "user": {"id": USER},
            "view": {"callback_id": "prepare_operator_correlation_resolution",
                     "private_metadata": json.dumps({"review_token": token}), "state": {"values": {
                "resolution": {"selected_resolution": {"selected_option": {"value": "22222222-2222-4222-8222-222222222222"}}},
                "verification": {"verification_basis": {"selected_option": {"value": "customer_confirmation"}}},
            }}},
        }
        prepare_payload["view"]["id"] = "V12345678"
        prepare_payload["view"]["hash"] = "1789000000.abc"
        started = time.monotonic()
        response = _signed(client, prepare_payload, timestamp="1789000001")
        elapsed = time.monotonic() - started
    assert elapsed < 0.75
    assert response.status_code == 200
    assert response.json()["response_action"] == "update"
    assert response.json()["view"]["callback_id"] == "operator_correlation_resolution_processing"
    session = store.get_review_session(review_token=token)
    assert session is not None and session.state in {"preparing", "prepared"}
    assert session.idempotency_key is not None
    if operator.prepare_calls:
        assert session.idempotency_key == operator.prepare_calls[0]["idempotency_key"]
    assert len(operator.prepare_calls) <= 1


def test_replay_and_terminal_session_retention_is_bounded(tmp_path) -> None:
    store = NotificationStore(tmp_path / "connector.sqlite3")
    store.initialize()
    with __import__("sqlite3").connect(tmp_path / "connector.sqlite3") as connection:
        old = "2020-01-01T00:00:00+00:00"
        for number in range(25):
            connection.execute(
                "INSERT INTO interaction_replays VALUES (?, 'responded', 200, '{}', ?, ?)",
                (f"{number:064x}", old, old),
            )
    store.prune_interaction_history(now_epoch=1789000000, max_replays=10,
                                    replay_retention_seconds=86400)
    assert store.interaction_replay_count() <= 10


def test_confirm_retry_recovers_ambiguous_remote_success_and_updates_root_once(tmp_path) -> None:
    operator = AmbiguousConfirmOperator()
    app, _store, slack, _operator = _app(tmp_path, operator=operator)
    with TestClient(app) as client:
        open_payload = {
            "type": "block_actions", "team": {"id": TEAM}, "user": {"id": USER},
            "channel": {"id": CHANNEL}, "message": {"ts": TS}, "trigger_id": "123.456.valid",
            "actions": [{"action_id": "review_operator_correlation", "value": CASE}],
        }
        assert _signed(client, open_payload).status_code == 200
        token = _wait_for_open_view(slack)
        prepare_payload = {
            "type": "view_submission", "team": {"id": TEAM}, "user": {"id": USER},
            "view": {"callback_id": "prepare_operator_correlation_resolution",
                     "id": "V12345678", "hash": "1789000000.abc",
                     "private_metadata": json.dumps({"review_token": token}), "state": {"values": {
                "resolution": {"selected_resolution": {"selected_option": {"value": "22222222-2222-4222-8222-222222222222"}}},
                "verification": {"verification_basis": {"selected_option": {"value": "customer_confirmation"}}},
            }}},
        }
        assert _signed(client, prepare_payload, timestamp="1789000001").status_code == 200
        deadline = time.monotonic() + 2
        while not slack.view_updates and time.monotonic() < deadline:
            time.sleep(0.02)
        confirm_payload = {
            "type": "view_submission", "team": {"id": TEAM}, "user": {"id": USER},
            "view": {"id": "V12345678", "hash": "1789000001.def", "callback_id": "confirm_operator_correlation_resolution",
                     "private_metadata": json.dumps({"review_token": token}), "state": {"values": {}}},
        }
        first = _signed(client, confirm_payload, timestamp="1789000002")
        deadline = time.monotonic() + 2
        while not slack.updates and time.monotonic() < deadline:
            time.sleep(0.02)
    assert first.json()["response_action"] == "update"
    assert first.json()["view"]["callback_id"] == "operator_correlation_resolution_processing"
    assert len(operator.confirm_calls) == 2
    assert operator.confirm_calls[0] == operator.confirm_calls[1]
    assert len(slack.updates) == 1


def test_restart_fails_request_started_view_open_without_retrying_trigger(tmp_path) -> None:
    store = NotificationStore(tmp_path / "connector.sqlite3")
    _accepted_correlation(store)
    binding = store.find_correlation_binding(
        tenant_ref="johanna", team_id=TEAM, channel_id=CHANNEL, message_ts=TS,
    )
    assert binding is not None
    admitted = store.admit_open_interaction(
        fingerprint="b" * 64, binding=binding, team_id=TEAM,
        slack_user_id=USER, trigger_id="123.456.short-lived",
        expires_at=1789000900,
    )
    token = admitted[3]
    assert token is not None
    store.mark_open_request_started(review_token=token)

    store.recover_incomplete()

    assert store.pending_opening_jobs() == []
    session = store.get_review_session(review_token=token)
    assert session is not None and session.state == "failed"


def test_open_replay_and_session_admission_roll_back_together(tmp_path, monkeypatch) -> None:
    store = NotificationStore(tmp_path / "connector.sqlite3")
    _accepted_correlation(store)
    binding = store.find_correlation_binding(
        tenant_ref="johanna", team_id=TEAM, channel_id=CHANNEL, message_ts=TS,
    )
    assert binding is not None

    def fail_replay(*args, **kwargs) -> None:
        raise RuntimeError("admission_failpoint")

    monkeypatch.setattr(store, "_insert_completed_interaction", fail_replay)
    with pytest.raises(RuntimeError, match="admission_failpoint"):
        store.admit_open_interaction(
            fingerprint="c" * 64, binding=binding, team_id=TEAM,
            slack_user_id=USER, trigger_id="123.456.short-lived",
            expires_at=1789000900,
        )
    assert _interaction_row_counts(tmp_path / "connector.sqlite3") == (0, 0)
    with __import__("sqlite3").connect(tmp_path / "connector.sqlite3") as connection:
        assert connection.execute(
            "SELECT count(*) FROM correlation_opening_jobs"
        ).fetchone()[0] == 0


def test_prepare_ack_does_not_await_cancellation_suppressing_operator(tmp_path) -> None:
    operator = CancellationSuppressingOperator("prepare")
    app, store, slack, _operator = _app(tmp_path, operator=operator)
    open_payload = {
        "type": "block_actions", "team": {"id": TEAM}, "user": {"id": USER},
        "channel": {"id": CHANNEL}, "message": {"ts": TS},
        "trigger_id": "123.456.valid",
        "actions": [{"action_id": "review_operator_correlation", "value": CASE}],
    }
    with TestClient(app) as client:
        assert _signed(client, open_payload).status_code == 200
        token = _wait_for_open_view(slack)
        payload = {
            "type": "view_submission", "team": {"id": TEAM}, "user": {"id": USER},
            "view": {"id": "V12345678", "hash": "1789000000.abc",
                     "callback_id": "prepare_operator_correlation_resolution",
                     "private_metadata": json.dumps({"review_token": token}),
                     "state": {"values": {
                         "resolution": {"selected_resolution": {"selected_option": {
                             "value": "22222222-2222-4222-8222-222222222222"}}},
                         "verification": {"verification_basis": {"selected_option": {
                             "value": "customer_confirmation"}}},
                     }}},
        }
        started = time.monotonic()
        response = _signed(client, payload, timestamp="1789000001")
        elapsed = time.monotonic() - started
        assert elapsed < 0.75
        assert response.json()["response_action"] == "update"
        session = store.get_review_session(review_token=token)
        assert session is not None and session.state == "preparing"
        operator.release.set()


def test_session_capacity_pruning_preserves_unexpired_open_and_prepared_work(tmp_path) -> None:
    store = NotificationStore(tmp_path / "connector.sqlite3")
    _accepted_correlation(store)
    binding = store.find_correlation_binding(
        tenant_ref="johanna", team_id=TEAM, channel_id=CHANNEL, message_ts=TS,
    )
    assert binding is not None
    opened = store.create_review_session(
        binding=binding, team_id=TEAM, slack_user_id=USER, expires_at=1789000900,
    )
    prepared = store.create_review_session(
        binding=binding, team_id=TEAM, slack_user_id=USER, expires_at=1788999999,
    )
    store.begin_prepare(
        review_token=prepared.review_token, action="close_without_match",
        candidate_id=None, verification_basis="no_valid_candidate_after_review",
        view_id="V12345678",
    )
    store.finish_prepare(
        review_token=prepared.review_token,
        command={"command_id": "33333333-3333-4333-8333-333333333333"},
    )

    store.prune_interaction_history(now_epoch=1789000000, max_sessions=1)

    assert store.get_review_session(review_token=opened.review_token) is not None
    retained = store.get_review_session(review_token=prepared.review_token)
    assert retained is not None and retained.state == "prepared"


def test_prepare_ack_does_not_await_cancellation_suppressing_slack_update(tmp_path) -> None:
    store = NotificationStore(tmp_path / "connector.sqlite3")
    _accepted_correlation(store)
    slack = CancellationSuppressingUpdateSlack()
    operator = FakeOperator()
    app = create_app(
        SlackConnectorSettings(
            interactions_enabled=True, bot_token="xoxb-synthetic",
            signing_secret=SECRET, team_id=TEAM,
            tenant_channels={"johanna": CHANNEL},
            tenant_operator_user_ids={"johanna": frozenset({USER})},
            operator_backends={"johanna": {"base_url": "https://operator.example", "read_token": "r" * 32, "write_token": "w" * 32}},
            storage_path=str(tmp_path / "connector.sqlite3"),
        ),
        store=store, slack_client=slack,
        operator_clients={"johanna": operator}, epoch_clock=lambda: 1789000000,
    )
    open_payload = {
        "type": "block_actions", "team": {"id": TEAM}, "user": {"id": USER},
        "channel": {"id": CHANNEL}, "message": {"ts": TS},
        "trigger_id": "123.456.valid",
        "actions": [{"action_id": "review_operator_correlation", "value": CASE}],
    }
    with TestClient(app) as client:
        assert _signed(client, open_payload).status_code == 200
        token = _wait_for_open_view(slack)
        payload = {
            "type": "view_submission", "team": {"id": TEAM}, "user": {"id": USER},
            "view": {"id": "V12345678", "hash": "1789000000.abc",
                     "callback_id": "prepare_operator_correlation_resolution",
                     "private_metadata": json.dumps({"review_token": token}),
                     "state": {"values": {
                         "resolution": {"selected_resolution": {"selected_option": {
                             "value": "22222222-2222-4222-8222-222222222222"}}},
                         "verification": {"verification_basis": {"selected_option": {
                             "value": "customer_confirmation"}}},
                     }}},
        }
        started = time.monotonic()
        response = _signed(client, payload, timestamp="1789000001")
        elapsed = time.monotonic() - started
        assert elapsed < 0.75
        assert response.json()["response_action"] == "update"
        slack.release.set()
