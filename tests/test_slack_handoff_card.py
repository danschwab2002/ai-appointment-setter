"""HND-001 card: no opaque case code in the headline, one link button to the conversation.

Measured on 2026-09-26 13:39Z: the first real HND-001 card reached #feed-chatwoot-johanna
as ``[p2] Nueva derivación · Johanna · C-968F2D48-4D52-4683-8215-587A4716F03E`` with
``component=chatwoot.conversation.185``. The team asked for the code to go and for a
button that opens the conversation. Everything upstream (bridge queue, admission,
threading by ``subject_ref``) stays exactly as it is.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
import hmac
import json
from urllib.parse import urlencode

from fastapi.testclient import TestClient
import pytest

from slack_correlation.app import SlackConnectorSettings, create_app
from slack_correlation.catalog import (
    HANDOFF_CONVERSATION_LINK_ACTION_ID,
    NotificationCommand,
    render_message,
)
from slack_correlation.client import SlackMessageReference
from slack_correlation.store import NotificationStore
from slack_correlation.worker import NotificationWorker

BASE_URL = "https://chat.example.test/app/accounts/1/conversations"
REAL_CASE = "C-968F2D48-4D52-4683-8215-587A4716F03E"
TEAM = "T0SYNTHETIC"
USER = "U0OPERATOR"
CHANNEL = "C0C0YEACVT2"
SECRET = "synthetic-signing-secret"


def _handoff(**overrides: object) -> NotificationCommand:
    values: dict[str, object] = {
        "event_id": "b32eb090-1a3b-5109-b48f-bb568392ffbe",
        "event_code": "HND-001",
        "dedupe_key": "a" * 64,
        "occurred_at": datetime(2026, 9, 26, 13, 39, 52, tzinfo=UTC),
        "subject_ref": REAL_CASE,
        "reason_code": "explicit_human_request",
        "component": "chatwoot.conversation.185",
        "state": "pending",
    }
    values.update(overrides)
    return NotificationCommand(**values)  # type: ignore[arg-type]


def _button(message: dict) -> dict | None:
    last = message["blocks"][-1]
    if last["type"] != "actions":
        return None
    assert len(last["elements"]) == 1
    return last["elements"][0]


# --- render -----------------------------------------------------------------


def test_handoff_card_drops_the_case_code_and_keeps_the_machine_fields() -> None:
    rendered = render_message(_handoff(), tenant_label="Johanna")

    assert rendered["text"] == "[p2] Nueva derivación · Johanna"
    assert rendered["blocks"][0] == {
        "type": "header",
        "text": {"type": "plain_text", "text": "[p2] Nueva derivación · Johanna"},
    }
    assert "968F2D48" not in repr(rendered)
    fields = repr(rendered["blocks"][1])
    for value in (
        "HND-001",
        "2026-09-26T13:39:52Z",
        "explicit_human_request",
        "chatwoot.conversation.185",
        "pending",
    ):
        assert value in fields
    assert rendered["metadata"] == {
        "event_type": "supportmagician_operational_event",
        "event_payload": {
            "event_id": "b32eb090-1a3b-5109-b48f-bb568392ffbe",
            "event_code": "HND-001",
        },
    }
    assert _button(rendered) is None


@pytest.mark.parametrize("base_url", [BASE_URL, BASE_URL + "/"])
def test_handoff_card_links_to_the_chatwoot_conversation(base_url: str) -> None:
    rendered = render_message(
        _handoff(), tenant_label="Johanna", conversation_base_url=base_url
    )

    assert _button(rendered) == {
        "type": "button",
        "action_id": HANDOFF_CONVERSATION_LINK_ACTION_ID,
        "text": {"type": "plain_text", "text": "Ir a la conversación"},
        "url": f"{BASE_URL}/185",
    }
    assert repr(rendered).count(f"{BASE_URL}/185") == 1
    assert rendered["text"] == "[p2] Nueva derivación · Johanna"
    assert "968F2D48" not in repr(rendered)


@pytest.mark.parametrize(
    "component",
    [None, "chatwoot_projection", "chatwoot.conversation.0", "chatwoot.conversation.x1"],
)
def test_handoff_card_has_no_link_without_a_chatwoot_conversation(
    component: str | None,
) -> None:
    rendered = render_message(
        _handoff(component=component),
        tenant_label="Johanna",
        conversation_base_url=BASE_URL,
    )

    assert _button(rendered) is None
    assert BASE_URL not in repr(rendered)


@pytest.mark.parametrize(
    "base_url",
    [
        "",
        "http://chat.example.test/app/accounts/1/conversations",
        "https://chat.example.test",
        "https://chat.example.test/",
        "https://user:secret@chat.example.test/app/accounts/1/conversations",
        "https://chat.example.test:8443/app/accounts/1/conversations",
        "https://chat.example.test/app/accounts/1/conversations?token=1",
        "https://chat.example.test/app/accounts/1/conversations#x",
        "https://chat.example.test/app/accounts/1/conversations <b>",
    ],
)
def test_handoff_card_rejects_an_unsafe_conversation_base_url(base_url: str) -> None:
    with pytest.raises(ValueError, match="invalid_conversation_base_url"):
        render_message(_handoff(), tenant_label="Johanna", conversation_base_url=base_url)


def test_other_handoff_codes_keep_the_generic_card_with_the_case_code() -> None:
    rendered = render_message(
        _handoff(event_code="HND-002"),
        tenant_label="Johanna",
        conversation_base_url=BASE_URL,
    )

    assert rendered["text"] == (
        f"[p2] Derivación sin destino disponible · Johanna · {REAL_CASE}"
    )
    assert _button(rendered) is None
    assert BASE_URL not in repr(rendered)


def test_handoff_card_keeps_its_thread() -> None:
    rendered = render_message(
        _handoff(),
        tenant_label="Johanna",
        thread_ts="1788800000.000001",
        conversation_base_url=BASE_URL,
    )

    assert rendered["thread_ts"] == "1788800000.000001"
    assert _button(rendered) is not None


# --- worker -----------------------------------------------------------------


class _AcceptedSlackClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def post_message(
        self, *, channel_id: str, message: dict
    ) -> SlackMessageReference:
        self.calls.append((channel_id, message))
        return SlackMessageReference(channel_id=channel_id, message_ts="1788800000.000001")


def _run_worker(tmp_path, *, tenant_conversation_base_urls: dict[str, str] | None):
    store = NotificationStore(tmp_path / "slack.sqlite3")
    store.initialize()
    command = _handoff()
    store.admit(tenant_ref="johanna", command=command)
    slack = _AcceptedSlackClient()
    worker = NotificationWorker(
        store=store,
        slack_client=slack,
        tenant_channels={"johanna": CHANNEL},
        tenant_labels={"johanna": "Johanna", "att1": "ATT1"},
        tenant_conversation_base_urls=tenant_conversation_base_urls,
        worker_id="slack-worker-1",
    )
    assert asyncio.run(worker.run_once()) is True
    stored = store.get(tenant_ref="johanna", notification_id=command.event_id)
    assert stored is not None and stored.state == "accepted"
    assert len(slack.calls) == 1
    return slack.calls[0]


def test_worker_builds_the_link_from_the_tenant_base_url(tmp_path) -> None:
    channel_id, message = _run_worker(
        tmp_path, tenant_conversation_base_urls={"johanna": BASE_URL}
    )

    assert channel_id == CHANNEL
    assert message["text"] == "[p2] Nueva derivación · Johanna"
    assert _button(message)["url"] == f"{BASE_URL}/185"


def test_worker_without_the_tenant_base_url_sends_the_card_without_a_button(
    tmp_path,
) -> None:
    channel_id, message = _run_worker(tmp_path, tenant_conversation_base_urls=None)

    assert channel_id == CHANNEL
    assert message["text"] == "[p2] Nueva derivación · Johanna"
    assert _button(message) is None


# --- settings ---------------------------------------------------------------


def test_settings_parse_tenant_scoped_conversation_base_urls(monkeypatch) -> None:
    monkeypatch.setenv(
        "SLACK_TENANT_CONVERSATION_BASE_URLS_JSON", json.dumps({"johanna": BASE_URL})
    )

    settings = SlackConnectorSettings.from_env()

    assert settings.tenant_conversation_base_urls == {"johanna": BASE_URL}


def test_settings_without_the_variable_have_no_conversation_base_urls(
    monkeypatch,
) -> None:
    monkeypatch.delenv("SLACK_TENANT_CONVERSATION_BASE_URLS_JSON", raising=False)

    assert SlackConnectorSettings.from_env().tenant_conversation_base_urls == {}


def test_connector_accepts_a_routable_https_conversation_base_url() -> None:
    create_app(
        SlackConnectorSettings(
            tenant_channels={"johanna": CHANNEL},
            tenant_conversation_base_urls={"johanna": BASE_URL},
        )
    )


@pytest.mark.parametrize(
    "mapping",
    [
        {"johanna": "http://chat.example.test/app/accounts/1/conversations"},
        {"johanna": "https://chat.example.test"},
        {"johanna": "https://chat.example.test/app/accounts/1/conversations?x=1"},
        {"johanna": "https://user:secret@chat.example.test/app/accounts/1/conversations"},
        {"att1": BASE_URL},
    ],
)
def test_connector_rejects_unsafe_or_unroutable_conversation_base_urls(mapping) -> None:
    with pytest.raises(ValueError, match="invalid_tenant_conversation_base_urls"):
        create_app(
            SlackConnectorSettings(
                tenant_channels={"johanna": CHANNEL},
                tenant_conversation_base_urls=mapping,
            )
        )


# --- the click on the button --------------------------------------------------


class _Slack:
    def __init__(self) -> None:
        self.views: list[dict] = []
        self.updates: list[dict] = []

    async def verify_auth(self, *, expected_team_id: str) -> None:
        assert expected_team_id == TEAM

    async def open_view(self, *, trigger_id: str, view: dict, expected_team_id: str) -> None:
        self.views.append(view)

    async def update_message(self, *, channel_id: str, message_ts: str, message: dict):
        self.updates.append(message)

    async def update_view(
        self, *, view_id: str, view_hash: str | None, view: dict, expected_team_id: str
    ) -> None:
        self.views.append(view)


class _Operator:
    def __init__(self) -> None:
        self.calls = 0

    async def get_case(self, case_id: str) -> dict:
        self.calls += 1
        raise AssertionError("a link click never reaches the operator backend")

    async def prepare(self, **kwargs: object) -> dict:
        raise AssertionError("a link click never reaches the operator backend")

    async def confirm(self, **kwargs: object) -> dict:
        raise AssertionError("a link click never reaches the operator backend")


def _interactions_app(tmp_path):
    store = NotificationStore(tmp_path / "connector.sqlite3")
    store.initialize()
    slack = _Slack()
    operator = _Operator()
    app = create_app(
        SlackConnectorSettings(
            interactions_enabled=True,
            bot_token="xoxb-synthetic",
            signing_secret=SECRET,
            team_id=TEAM,
            tenant_channels={"johanna": CHANNEL},
            tenant_operator_user_ids={"johanna": frozenset({USER})},
            operator_backends={
                "johanna": {
                    "base_url": "https://operator.example",
                    "read_token": "r" * 32,
                    "write_token": "w" * 32,
                }
            },
            storage_path=str(tmp_path / "connector.sqlite3"),
        ),
        store=store,
        slack_client=slack,
        operator_clients={"johanna": operator},
        epoch_clock=lambda: 1789000000,
    )
    return app, slack, operator


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


def _link_click(*, user: str = USER, team: str = TEAM) -> dict:
    """What Slack posts when someone presses the url button of a delivered card."""
    return {
        "type": "block_actions",
        "team": {"id": team},
        "user": {"id": user},
        "channel": {"id": CHANNEL},
        "message": {
            "ts": "1788800000.000001",
            "metadata": {
                "event_type": "supportmagician_operational_event",
                "event_payload": {
                    "event_id": "b32eb090-1a3b-5109-b48f-bb568392ffbe",
                    "event_code": "HND-001",
                },
            },
        },
        "trigger_id": "123.456.valid",
        "actions": [
            {
                "type": "button",
                "block_id": "abc",
                "action_id": HANDOFF_CONVERSATION_LINK_ACTION_ID,
                "text": {"type": "plain_text", "text": "Ir a la conversación"},
                "url": f"{BASE_URL}/185",
                "action_ts": "1789000000.000100",
            }
        ],
    }


def test_link_button_click_is_acknowledged_without_side_effects(tmp_path) -> None:
    app, slack, operator = _interactions_app(tmp_path)

    with TestClient(app) as client:
        first = _signed(client, _link_click())
        anyone = _signed(client, _link_click(user="U0ANYONE"))

    assert first.status_code == anyone.status_code == 200
    assert first.json() == anyone.json() == {}
    assert slack.views == [] and slack.updates == []
    assert operator.calls == 0


def test_link_button_click_from_another_workspace_is_rejected(tmp_path) -> None:
    app, slack, operator = _interactions_app(tmp_path)

    with TestClient(app) as client:
        response = _signed(client, _link_click(team="T0OTHER"))

    assert response.status_code == 400
    assert slack.views == [] and operator.calls == 0
