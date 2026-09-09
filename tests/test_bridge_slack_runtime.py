import asyncio
from dataclasses import replace
from pathlib import Path
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from bridge.app import Settings, create_app
from bridge.commercial_ally import JOHANNA_COMMERCIAL_ALLY
from bridge.slack_runtime import SlackBridgeRuntime, create_slack_bridge_runtime
from slack_correlation.catalog import NotificationCommand
from slack_correlation.producer import AdmissionReceipt


class _ClosableProducer:
    def __init__(self) -> None:
        self.commands: list[NotificationCommand] = []
        self.closed = False

    async def admit(self, command: NotificationCommand) -> AdmissionReceipt:
        self.commands.append(command)
        return AdmissionReceipt("admitted", command.event_id, "pending")

    async def aclose(self) -> None:
        self.closed = True


class _ProjectionStore:
    def __init__(self) -> None:
        self.claim_calls: list[dict[str, object]] = []
        self.binding_checks: list[object] = []

    async def resolve_commercial_ally_runtime_binding(self, expected):
        self.binding_checks.append(expected)
        return expected

    async def claim_slack_correlation_notifications(
        self, **kwargs: object
    ) -> list[object]:
        self.claim_calls.append(kwargs)
        return []

    async def complete_slack_correlation_notification(self, **kwargs: object) -> None:
        raise AssertionError("no claim should complete")

    async def release_slack_correlation_notification(self, **kwargs: object) -> None:
        raise AssertionError("no claim should release")


def test_connector_runtime_configuration_is_all_or_nothing_and_closes() -> None:
    assert create_slack_bridge_runtime(base_url=None, bearer_token=None) is None
    with pytest.raises(ValueError, match="slack_connector_configuration_incomplete"):
        create_slack_bridge_runtime(
            base_url="https://connector.example.com",
            bearer_token=None,
            expected_tenant_ref="johanna",
        )

    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            202,
            json={
                "status": "admitted",
                "tenant_ref": "johanna",
                "notification_id": "unused",
                "delivery_state": "pending",
            },
        )
    )
    runtime = create_slack_bridge_runtime(
        base_url="https://connector.example.com",
        bearer_token="a" * 32,
        expected_tenant_ref="johanna",
        transport=transport,
    )
    assert runtime is not None
    asyncio.run(runtime.aclose())


def test_runtime_closes_an_injected_producer() -> None:
    producer = _ClosableProducer()
    runtime = SlackBridgeRuntime(producer=producer)

    asyncio.run(runtime.aclose())

    assert producer.closed is True


def test_app_closes_the_runtime_it_constructs(monkeypatch: pytest.MonkeyPatch) -> None:
    producer = _ClosableProducer()
    runtime = SlackBridgeRuntime(producer=producer)
    monkeypatch.setattr(
        "bridge.app.create_slack_bridge_runtime",
        lambda **kwargs: runtime,
    )
    app = create_app(
        Settings(
            webhook_secret="unused",
            allowed_jid="unused@s.whatsapp.net",
            capture_dir=Path("/tmp/slack-bridge-runtime-test"),
            max_age_seconds=300,
            slack_connector_projection_enabled=True,
            slack_connector_base_url="https://connector.example.com",
            slack_connector_bearer_token="a" * 32,
            slack_connector_worker_id="johanna-slack-1",
        ),
        supabase_client=_ProjectionStore(),  # type: ignore[arg-type]
    )

    with TestClient(app):
        pass

    assert producer.closed is True


@pytest.mark.parametrize(
    ("config", "manifest_path", "tenant_ref", "funnel_ref"),
    [
        (JOHANNA_COMMERCIAL_ALLY, None, "lancemos", "psicologajohanna"),
        (
            replace(
                JOHANNA_COMMERCIAL_ALLY,
                tenant_ref="att1",
                funnel_ref="att1-main",
                ally_ref="att1",
            ),
            Path("/runtime/att1.json"),
            "att1",
            "att1-main",
        ),
    ],
)
def test_app_lifecycle_wires_one_scoped_projection_worker_for_each_bridge(
    config: object,
    manifest_path: Path | None,
    tenant_ref: str,
    funnel_ref: str,
) -> None:
    store = _ProjectionStore()
    producer = _ClosableProducer()
    runtime = SlackBridgeRuntime(producer=producer)
    settings = Settings(
        webhook_secret="unused",
        allowed_jid="unused@s.whatsapp.net",
        capture_dir=Path("/tmp/slack-bridge-runtime-test"),
        max_age_seconds=300,
        commercial_ally_config=config,  # type: ignore[arg-type]
        commercial_ally_manifest_path=manifest_path,
        slack_connector_base_url="https://connector.example.com",
        slack_connector_bearer_token="a" * 32,
        slack_connector_projection_enabled=True,
        slack_connector_worker_id=f"{tenant_ref}-slack-1",
    )
    app = create_app(
        settings,
        supabase_client=store,  # type: ignore[arg-type]
        slack_runtime=runtime,
    )

    with TestClient(app):
        deadline = time.monotonic() + 1
        while not store.claim_calls and time.monotonic() < deadline:
            time.sleep(0.01)
        assert app.state.slack_projection_worker is not None
        assert store.claim_calls
        assert store.claim_calls[0]["tenant_ref"] == tenant_ref
        assert store.claim_calls[0]["funnel_ref"] == funnel_ref
        assert store.claim_calls[0]["binding_version"] == (
            None if manifest_path is None else config.binding_version
        )
        if manifest_path is None:
            assert store.binding_checks == []
        else:
            assert store.binding_checks == [config]
        assert runtime.producer is producer

    assert producer.closed is False


def test_portable_binding_is_attested_before_projection_worker_starts() -> None:
    class DriftedBindingStore(_ProjectionStore):
        async def resolve_commercial_ally_runtime_binding(self, expected):
            raise RuntimeError("binding drift")

    store = DriftedBindingStore()
    runtime = SlackBridgeRuntime(producer=_ClosableProducer())
    config = replace(
        JOHANNA_COMMERCIAL_ALLY,
        tenant_ref="att1",
        funnel_ref="att1-main",
        ally_ref="att1",
    )
    app = create_app(
        Settings(
            webhook_secret="unused",
            allowed_jid="unused@s.whatsapp.net",
            capture_dir=Path("/tmp/slack-bridge-runtime-test"),
            max_age_seconds=300,
            commercial_ally_config=config,
            commercial_ally_manifest_path=Path("/runtime/att1.json"),
            slack_connector_projection_enabled=True,
            slack_connector_base_url="https://connector.example.com",
            slack_connector_bearer_token="a" * 32,
            slack_connector_worker_id="att1-slack-1",
        ),
        supabase_client=store,  # type: ignore[arg-type]
        slack_runtime=runtime,
    )

    with pytest.raises(RuntimeError, match="binding drift"):
        with TestClient(app):
            pass
    assert store.claim_calls == []


def test_projection_is_default_off_and_partial_connector_config_fails_closed() -> None:
    base = Settings(
        webhook_secret="unused",
        allowed_jid="unused@s.whatsapp.net",
        capture_dir=Path("/tmp/slack-bridge-runtime-test"),
        max_age_seconds=300,
    )
    app = create_app(base, supabase_client=_ProjectionStore())  # type: ignore[arg-type]
    assert app.state.slack_projection_worker is None

    partial = replace(base, slack_connector_base_url="https://connector.example.com")
    with pytest.raises(ValueError, match="slack_connector_configuration_incomplete"):
        create_app(partial, supabase_client=_ProjectionStore())  # type: ignore[arg-type]


def test_halted_projection_worker_fails_readiness() -> None:
    store = _ProjectionStore()
    runtime = SlackBridgeRuntime(producer=_ClosableProducer())
    settings = Settings(
        webhook_secret="unused",
        allowed_jid="unused@s.whatsapp.net",
        capture_dir=Path("/tmp/slack-bridge-runtime-test"),
        max_age_seconds=300,
        slack_connector_projection_enabled=True,
        slack_connector_base_url="https://connector.example.com",
        slack_connector_bearer_token="a" * 32,
        slack_connector_worker_id="johanna-slack-1",
    )
    app = create_app(
        settings,
        supabase_client=store,  # type: ignore[arg-type]
        slack_runtime=runtime,
    )

    with TestClient(app) as client:
        app.state.slack_projection_worker._halted = True
        response = client.get("/ready")
        app.state.slack_projection_worker._halted = False
        app.state.slack_projection_worker._healthy = False
        unhealthy_response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"detail": "slack_projection_halted"}
    assert unhealthy_response.status_code == 503
    assert unhealthy_response.json() == {"detail": "slack_projection_unhealthy"}
