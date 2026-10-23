"""Un conector de Slack por instancia (F-133, E-027).

Cada instancia nueva corre su propio conector en su stack, con un solo tenant en los
mapas `SLACK_TENANT_*_JSON` (lo que #257 ya aceptaba) y hablandole al bridge de su
stack por la red interna. Hasta el 2026-10-23 eso lo impedian dos candados: el canal
de Johanna fijo en el codigo y el https obligatorio hacia el bridge. Estas pruebas
fallan sin el cambio:

- un tenant que no es Johanna arranca desde el entorno y resuelve una interaccion
  firmada contra el bridge interno (`http://appointment-bridge:8000`);
- ese http se acepta solo con `SLACK_OPERATOR_INTERNAL_HOST` exacto, y un http hacia
  afuera se rechaza aunque la variable este;
- el bridge le puede hablar al conector de su stack solo con
  `SLACK_CONNECTOR_INTERNAL_HOST`;
- la configuracion del conector compartido (Johanna y ATT1, https) arranca igual.

Deuda declarada (regla 2 de la politica): no hay payloads de Slack capturados en
`tests/fixtures/`. Las interacciones siguen el precedente del repo
(`tests/test_slack_correlation_interactions.py`, payloads con la forma de Slack
escritos a mano y firmados con un secreto sintetico). El fixture real sale del dia 0
de la primera instancia, con la app creada desde el manifiesto.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
import hmac
import json
from pathlib import Path
import time
from urllib.parse import urlencode

import httpx
from fastapi.testclient import TestClient
import pytest

from slack_correlation import operator_client as operator_client_module
from slack_correlation.app import SlackConnectorSettings, create_app
from slack_correlation.catalog import NotificationCommand
from slack_correlation.operator_client import OperatorBridgeClient
from slack_correlation.producer import SlackConnectorProducer
from slack_correlation.store import NotificationStore

ROOT = Path(__file__).parents[1]
TEAM = "T0MARIBEL1"
CHANNEL = "C0MARIBEL01"
TENANT = "maribel"
USER = "U0MARIBEL01"
CASE = "11111111-1111-4111-8111-111111111111"
TS = "1789000000.000001"
SECRET = "synthetic-signing-secret"
BRIDGE = "http://appointment-bridge:8000"
BRIDGE_HOST = "appointment-bridge:8000"
READ_TOKEN = "r" * 32
WRITE_TOKEN = "w" * 32


def _single_instance_env(monkeypatch, *, internal_host: str | None = BRIDGE_HOST,
                         base_url: str = BRIDGE, storage: Path) -> None:
    """El entorno de un conector por instancia: un solo tenant en cada mapa."""

    for name, value in {
        "SLACK_INTERACTIONS_ENABLED": "true",
        "SLACK_BOT_TOKEN": "xoxb-synthetic",
        "SLACK_SIGNING_SECRET": SECRET,
        "SLACK_TEAM_ID": TEAM,
        "SLACK_TENANT_CHANNELS_JSON": json.dumps({TENANT: CHANNEL}),
        "SLACK_TENANT_OPERATOR_USER_IDS_JSON": json.dumps({TENANT: [USER]}),
        "SLACK_TENANT_OPERATOR_BACKENDS_JSON": json.dumps({TENANT: {
            "base_url": base_url, "read_token": READ_TOKEN, "write_token": WRITE_TOKEN,
        }}),
        "SLACK_STORAGE_PATH": str(storage),
    }.items():
        monkeypatch.setenv(name, value)
    if internal_host is None:
        monkeypatch.delenv("SLACK_OPERATOR_INTERNAL_HOST", raising=False)
    else:
        monkeypatch.setenv("SLACK_OPERATOR_INTERNAL_HOST", internal_host)


class _FakeSlack:
    def __init__(self) -> None:
        self.views: list[tuple[str, dict]] = []

    async def verify_auth(self, *, expected_team_id: str) -> None:
        assert expected_team_id == TEAM

    async def open_view(self, *, trigger_id: str, view: dict, expected_team_id: str) -> None:
        assert expected_team_id == TEAM
        self.views.append((trigger_id, view))

    async def update_message(self, *, channel_id: str, message_ts: str, message: dict):
        return None

    async def update_view(self, *, view_id: str, view_hash: str | None, view: dict,
                          expected_team_id: str) -> None:
        return None


def _private_case() -> dict:
    return {
        "case_id": CASE,
        "event_type": "PURCHASE_APPROVED",
        "outcome": "unmatched",
        "candidate_count": 1,
        "automation_blocked": True,
        "identity": {"masked_email": "a***z@example.com", "masked_phone": "********4567"},
        "candidates": [{
            "purchase_intent_id": "22222222-2222-4222-8222-222222222222",
            "lifecycle_state": "waiting_for_purchase",
            "matched_by": ["email"],
            "submitted_at": "2026-09-09T11:55:00Z",
            "masked_email": "a***z@example.com",
            "masked_phone": "********4567",
        }],
    }


def _accepted_correlation(store: NotificationStore) -> None:
    store.initialize()
    command = NotificationCommand(
        event_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        event_code="COR-001",
        dedupe_key="a" * 64,
        occurred_at=datetime(2026, 9, 9, 12, tzinfo=UTC),
        subject_ref=f"C-{CASE}",
    )
    store.admit(tenant_ref=TENANT, command=command, channel_id=CHANNEL)
    claim = store.claim_next(worker_id="worker-1")
    assert claim is not None
    store.mark_request_started(claim)
    store.finalize_accepted(claim, channel_id=CHANNEL, message_ts=TS, thread_ts=None, team_id=TEAM)


def _signed(client: TestClient, payload: dict, *, secret: str = SECRET):
    timestamp = "1789000000"
    body = urlencode({"payload": json.dumps(payload, separators=(",", ":"))}).encode()
    signature = "v0=" + hmac.new(
        secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256
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


def _block_action() -> dict:
    return {
        "type": "block_actions",
        "team": {"id": TEAM},
        "user": {"id": USER},
        "channel": {"id": CHANNEL},
        "message": {"ts": TS},
        "trigger_id": "123.456.valid",
        "actions": [{"action_id": "review_operator_correlation", "value": CASE}],
    }


def test_a_tenant_other_than_johanna_boots_from_env_and_resolves_a_signed_interaction(
    tmp_path, monkeypatch,
) -> None:
    storage = tmp_path / "connector.sqlite3"
    _single_instance_env(monkeypatch, storage=storage)
    seen: list[httpx.Request] = []

    def bridge(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"case": _private_case()})

    real_client = operator_client_module.OperatorBridgeClient

    def through_the_stack_bridge(**kwargs):
        # El cliente real, con su validacion de URL; solo la red es falsa.
        return real_client(**kwargs, transport=httpx.MockTransport(bridge))

    monkeypatch.setattr(
        operator_client_module, "OperatorBridgeClient", through_the_stack_bridge
    )
    store = NotificationStore(storage)
    _accepted_correlation(store)
    slack = _FakeSlack()
    settings = SlackConnectorSettings.from_env()
    assert settings.operator_internal_host == BRIDGE_HOST

    app = create_app(settings, store=store, slack_client=slack, epoch_clock=lambda: 1789000000)
    with TestClient(app) as client:
        forged = _signed(client, _block_action(), secret="another-secret")
        response = _signed(client, _block_action())
        deadline = time.monotonic() + 2
        while not slack.views and time.monotonic() < deadline:
            time.sleep(0.02)

    assert forged.status_code == 401
    assert response.status_code == 200
    assert len(slack.views) == 1
    assert [str(request.url) for request in seen] == [
        f"{BRIDGE}/internal/operator/correlations/unresolved/{CASE}/private-review"
    ]
    assert seen[0].headers["Authorization"] == f"Bearer {READ_TOKEN}"


def test_the_stack_bridge_over_http_needs_the_explicit_variable(tmp_path, monkeypatch) -> None:
    _single_instance_env(monkeypatch, internal_host=None, storage=tmp_path / "c.sqlite3")

    with pytest.raises(ValueError, match="invalid_operator_backend_configuration"):
        create_app(SlackConnectorSettings.from_env(), slack_client=_FakeSlack())


@pytest.mark.parametrize(
    ("base_url", "internal_host"),
    [
        # http hacia afuera, aunque la variable este declarada.
        ("http://bridge.example.com:8000", BRIDGE_HOST),
        ("http://203.0.113.10:8000", BRIDGE_HOST),
        # otro servicio, otro puerto, sin puerto, con ruta o con usuario.
        ("http://otro-bridge:8000", BRIDGE_HOST),
        ("http://appointment-bridge:8001", BRIDGE_HOST),
        ("http://appointment-bridge", "appointment-bridge:80"),
        ("http://appointment-bridge:8000/api", BRIDGE_HOST),
        ("http://u:p@appointment-bridge:8000", BRIDGE_HOST),
    ],
)
def test_http_outside_the_declared_stack_service_is_rejected(
    tmp_path, monkeypatch, base_url: str, internal_host: str,
) -> None:
    _single_instance_env(
        monkeypatch, internal_host=internal_host, base_url=base_url,
        storage=tmp_path / "c.sqlite3",
    )

    with pytest.raises(ValueError, match="invalid_operator_(backend_configuration|bridge_base_url)"):
        create_app(SlackConnectorSettings.from_env(), slack_client=_FakeSlack())


@pytest.mark.parametrize(
    "internal_host",
    [
        "http://appointment-bridge:8000",
        "bridge.example.com:8000",
        "10.0.0.5:8000",
        "127:8000",
        "appointment-bridge",
        "appointment-bridge:0",
        "appointment-bridge:080",
        "appointment-bridge:70000",
        "Appointment-Bridge:8000",
    ],
)
def test_a_malformed_internal_host_is_rejected_even_with_interactions_off(
    internal_host: str,
) -> None:
    with pytest.raises(ValueError, match="invalid_operator_internal_host"):
        create_app(SlackConnectorSettings(operator_internal_host=internal_host))


def test_the_operator_client_takes_http_only_toward_the_declared_host() -> None:
    with pytest.raises(ValueError, match="invalid_operator_bridge_base_url"):
        OperatorBridgeClient(base_url=BRIDGE, read_bearer=READ_TOKEN, write_bearer=WRITE_TOKEN)
    with pytest.raises(ValueError, match="invalid_operator_bridge_base_url"):
        OperatorBridgeClient(
            base_url="http://otro-bridge:8000", read_bearer=READ_TOKEN,
            write_bearer=WRITE_TOKEN, internal_host=BRIDGE_HOST,
        )
    OperatorBridgeClient(
        base_url=BRIDGE, read_bearer=READ_TOKEN, write_bearer=WRITE_TOKEN,
        internal_host=BRIDGE_HOST,
    )


def test_the_bridge_reaches_its_stack_connector_only_with_the_variable(monkeypatch) -> None:
    monkeypatch.delenv("SLACK_CONNECTOR_INTERNAL_HOST", raising=False)
    token = "t" * 32
    with pytest.raises(ValueError, match="invalid_connector_base_url"):
        SlackConnectorProducer(
            base_url="http://slack-connector:8000", bearer_token=token,
            expected_tenant_ref=TENANT,
        )
    with pytest.raises(ValueError, match="invalid_connector_base_url"):
        SlackConnectorProducer(
            base_url="http://connector.example.com:8000", bearer_token=token,
            expected_tenant_ref=TENANT, internal_host="slack-connector:8000",
        )
    with pytest.raises(ValueError, match="invalid_connector_internal_host"):
        SlackConnectorProducer(
            base_url="http://connector.example.com:8000", bearer_token=token,
            expected_tenant_ref=TENANT, internal_host="connector.example.com:8000",
        )
    explicit = SlackConnectorProducer(
        base_url="http://slack-connector:8000", bearer_token=token,
        expected_tenant_ref=TENANT, internal_host="slack-connector:8000",
    )
    # Desde el entorno del bridge, sin cambiar el codigo del bridge.
    monkeypatch.setenv("SLACK_CONNECTOR_INTERNAL_HOST", "slack-connector:8000")
    from_env = SlackConnectorProducer(
        base_url="http://slack-connector:8000/", bearer_token=token,
        expected_tenant_ref=TENANT,
    )
    asyncio.run(explicit.aclose())
    asyncio.run(from_env.aclose())


def _env_example() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (ROOT / "deploy/slack-connector.env.example").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            name, _, value = line.partition("=")
            values[name] = value
    return values


def test_the_shared_connector_example_config_boots_unchanged(tmp_path, monkeypatch) -> None:
    example = _env_example()
    assert "SLACK_OPERATOR_INTERNAL_HOST" not in example
    for name, value in example.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("SLACK_OPERATOR_INTERNAL_HOST", raising=False)
    monkeypatch.setenv("SLACK_STORAGE_PATH", str(tmp_path / "c.sqlite3"))

    settings = SlackConnectorSettings.from_env()

    assert settings.operator_internal_host is None
    assert settings.tenant_channels == {"johanna": "C0C0YEACVT2"}
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 200


def test_the_shared_connector_with_johanna_and_att1_over_https_boots_unchanged(
    tmp_path, monkeypatch,
) -> None:
    """La forma del compartido: dos tenants, interacciones y backends https."""

    monkeypatch.delenv("SLACK_OPERATOR_INTERNAL_HOST", raising=False)
    for name, value in {
        "SLACK_INTERACTIONS_ENABLED": "true",
        "SLACK_BOT_TOKEN": "xoxb-synthetic",
        "SLACK_SIGNING_SECRET": SECRET,
        "SLACK_TEAM_ID": TEAM,
        "SLACK_TENANT_CHANNELS_JSON": json.dumps(
            {"johanna": "C0C0YEACVT2", "att1": "C0ATT1TEST01"}
        ),
        "SLACK_TENANT_OPERATOR_USER_IDS_JSON": json.dumps(
            {"johanna": ["U12345678"], "att1": ["U12345678"]}
        ),
        "SLACK_TENANT_OPERATOR_BACKENDS_JSON": json.dumps({
            "johanna": {"base_url": "https://infra-appointment-bridge.example.test",
                        "read_token": "j" * 32, "write_token": "k" * 32},
            "att1": {"base_url": "https://att1-bridge.example.test",
                     "read_token": "a" * 32, "write_token": "b" * 32},
        }),
        "SLACK_STORAGE_PATH": str(tmp_path / "c.sqlite3"),
    }.items():
        monkeypatch.setenv(name, value)

    settings = SlackConnectorSettings.from_env()

    assert settings.operator_internal_host is None
    with TestClient(create_app(settings, slack_client=_FakeSlack())) as client:
        assert client.get("/health").status_code == 200


def test_the_instance_manifest_turns_on_signed_interactivity_only() -> None:
    manifest = json.loads(
        (ROOT / "deploy/slack-app-manifest-instancia-v1.json").read_text()
    )
    settings = manifest["settings"]

    assert manifest["oauth_config"]["scopes"] == {"bot": ["chat:write"]}
    assert settings["socket_mode_enabled"] is False
    assert "event_subscriptions" not in settings
    assert settings["interactivity"]["is_enabled"] is True
    request_url = settings["interactivity"]["request_url"]
    # El host lo completa quien instala: el marcador no es una URL valida a proposito.
    assert request_url.startswith("https://<COMPLETAR-")
    assert request_url.endswith("/slack/interactions")
    assert "INSTANCIA" in request_url
    assert "INSTANCIA" in manifest["display_information"]["name"]
    assert "slash_commands" not in manifest.get("features", {})
