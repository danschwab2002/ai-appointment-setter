"""La revision diaria le habla al conector de Slack de la instancia por http interno.

Con el conector en el stack (F-133, #315), el bridge ya le habla por
http://<stack>_slack-connector:8000 con SLACK_CONNECTOR_INTERNAL_HOST. La revision
diaria validaba SLACK_CONNECTOR_BASE_URL como https por su cuenta
(daily_feedback_app.py, _validated_https_origin), y la ruta publica del conector solo
publica /slack/interactions: sin este cambio, la revision no podia llegar a /ready ni a
/internal/v1/notifications.

La regla es la del productor (slack_correlation.internal_http), con la misma variable:

- con SLACK_CONNECTOR_INTERNAL_HOST declarado, http solo hacia ese host:puerto exacto;
- un http hacia afuera (un dominio, una IP, otro servicio u otro puerto) se rechaza con
  el error de siempre, invalid_slack_connector_origin;
- sin la variable, todo igual que hoy (Johanna y ATT1, con el conector compartido por
  https);
- /ready la declara como la cuarta excepcion, slack_connector_internal_http.

La red es falsa (la de test_daily_feedback_app, con las capturas de Chatwoot de ATT1);
el repositorio, el colector y el productor son los que arma la fabrica real.
"""

from __future__ import annotations

from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from test_daily_feedback_app import (
    ATT1_EXCEPTIONS,
    MANUAL_RUN,
    InstanceNetwork,
    _att1_chatwoot,
    _att1_environment,
    _johanna_environment,
    _route_through,
    _with,
)

INTERNAL_HOST = "maribel_slack-connector:8000"
INTERNAL_URL = f"http://{INTERNAL_HOST}"


def _instance_environment(**changes: str | None) -> dict[str, str]:
    """El set de ATT1 con el conector de la instancia por http interno, como lo arma la
    plantilla (el nombre completo de Swarm, con guion bajo)."""
    return _with(
        _att1_environment(),
        {
            "SLACK_CONNECTOR_BASE_URL": INTERNAL_URL,
            "SLACK_CONNECTOR_INTERNAL_HOST": INTERNAL_HOST,
            **changes,
        },
    )


class InternalConnectorNetwork(InstanceNetwork):
    """La red de la instancia, con el conector en el stack por http."""

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == f"{INTERNAL_URL}/ready":
            self.requests.append(f"{request.method} {url}")
            assert request.headers["x-expected-tenant-ref"] == self.connector_tenant_ref
            return httpx.Response(200, json={"status": "ok"})
        return super().handle(request)


# --- Con la variable: http hacia el host declarado -----------------------------------


@pytest.mark.parametrize("url", [INTERNAL_URL, f"{INTERNAL_URL}/"])
def test_declared_internal_host_accepts_http_to_that_host(url: str) -> None:
    from bridge.daily_feedback_app import DailyFeedbackRuntimeSettings

    settings = DailyFeedbackRuntimeSettings.from_env(
        _instance_environment(SLACK_CONNECTOR_BASE_URL=url)
    )

    assert settings.slack_connector_base_url == INTERNAL_URL
    assert settings.slack_connector_internal_host == INTERNAL_HOST
    assert settings.exceptions == (*ATT1_EXCEPTIONS, "slack_connector_internal_http")


def test_internal_connector_is_the_only_exception_when_it_is_the_only_one_declared() -> None:
    from bridge.daily_feedback_app import DailyFeedbackRuntimeSettings

    settings = DailyFeedbackRuntimeSettings.from_env(
        _with(
            _johanna_environment(),
            {
                "SLACK_CONNECTOR_BASE_URL": "http://slack-connector:8000",
                "SLACK_CONNECTOR_INTERNAL_HOST": "slack-connector:8000",
            },
        )
    )

    assert settings.exceptions == ("slack_connector_internal_http",)


def test_the_real_factory_reaches_the_internal_connector_and_ready_declares_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bridge.daily_feedback_app import create_application_from_env

    # El proceso no la tiene: la revision usa lo que declara su entorno.
    monkeypatch.delenv("SLACK_CONNECTOR_INTERNAL_HOST", raising=False)
    network = InternalConnectorNetwork(
        supabase_origin="http://att1-gateway:8080",
        chatwoot=_att1_chatwoot(),
        connector_tenant_ref="att1",
    )
    _route_through(monkeypatch, network)

    app = create_application_from_env(_instance_environment())
    assert network.requests == []

    with TestClient(app) as client:
        started = list(network.requests)
        ready = client.get("/ready")
        run = client.post("/internal/v1/daily-feedback/run", headers=MANUAL_RUN)

    # El preflight del productor llega al conector por http interno.
    assert started == [
        "POST http://att1-gateway:8080/rest/v1/rpc/configure_daily_feedback_scope_v2",
        "GET https://chatwoot.example.test/api/v1/accounts/2/inboxes/11",
        "GET https://chatwoot.example.test/api/v1/accounts/2/inboxes/11/agent_bot",
        "GET https://chatwoot.example.test/api/v1/accounts/2/agent_bots/2",
        f"GET {INTERNAL_URL}/ready",
    ]
    assert ready.status_code == 200
    assert ready.json() == {
        "status": "ready",
        "daily_feedback": "staged",
        "exceptions": [*ATT1_EXCEPTIONS, "slack_connector_internal_http"],
    }
    assert run.status_code == 200
    # Las notificaciones (/internal/v1/notifications) salen del mismo cliente.
    producer = app.state.daily_feedback_scheduler._producer
    assert str(producer._client.base_url).rstrip("/") == INTERNAL_URL
    assert not any(request.startswith("GET https://connector.") for request in network.requests)


# --- Con la variable: http hacia afuera, rechazado -----------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://connector.example.test",  # un dominio publico
        "http://connector.example.test:8000",
        "http://10.0.0.5:8000",  # una IP
        "http://127.0.0.1:8000",
        "http://otro_slack-connector:8000",  # otro servicio
        "http://maribel_slack-connector:8001",  # otro puerto
        "http://maribel_slack-connector",  # sin puerto
        "http://maribel_slack-connector:8000/internal",  # con ruta
        "http://user@maribel_slack-connector:8000",  # con usuario
        "http://maribel_slack-connector:8000?x=1",
        "HTTP://maribel_slack-connector:8000",
        "ftp://maribel_slack-connector:8000",
    ],
)
def test_declared_internal_host_rejects_http_anywhere_else(url: str) -> None:
    from bridge.daily_feedback_app import DailyFeedbackRuntimeSettings

    with pytest.raises(ValueError, match="^invalid_slack_connector_origin$"):
        DailyFeedbackRuntimeSettings.from_env(
            _instance_environment(SLACK_CONNECTOR_BASE_URL=url)
        )


@pytest.mark.parametrize(
    "internal_host",
    [
        "connector.example.test:8000",  # un dominio no se puede declarar
        "10.0.0.5:8000",  # una IP tampoco
        "8000:8000",
        "maribel_slack-connector",  # sin puerto
        "maribel_slack-connector:0",
        "maribel_slack-connector:080",
        "Maribel_slack-connector:8000",
        "http://maribel_slack-connector:8000",
    ],
)
def test_malformed_internal_host_does_not_start(internal_host: str) -> None:
    from bridge.daily_feedback_app import DailyFeedbackRuntimeSettings

    with pytest.raises(ValueError, match="^invalid_slack_connector_internal_host$"):
        DailyFeedbackRuntimeSettings.from_env(
            _instance_environment(SLACK_CONNECTOR_INTERNAL_HOST=internal_host)
        )


def test_settings_built_by_hand_cannot_hold_an_undeclared_http_connector() -> None:
    from bridge.daily_feedback_app import DailyFeedbackRuntimeSettings

    settings = DailyFeedbackRuntimeSettings.from_env(_instance_environment())
    with pytest.raises(ValueError, match="^invalid_slack_connector_origin$"):
        replace(settings, slack_connector_internal_host="")
    with pytest.raises(ValueError, match="^invalid_slack_connector_origin$"):
        replace(settings, slack_connector_base_url="http://connector.example.test")
    with pytest.raises(ValueError, match="^invalid_slack_connector_internal_host$"):
        replace(settings, slack_connector_internal_host="connector.example.test:8000")


# --- Sin la variable: igual que hoy --------------------------------------------------


@pytest.mark.parametrize("internal_host", [None, "", "   "])
def test_without_the_variable_http_to_the_connector_is_rejected_as_today(
    monkeypatch: pytest.MonkeyPatch, internal_host: str | None
) -> None:
    from bridge.daily_feedback_app import DailyFeedbackRuntimeSettings

    # Ni siquiera si el proceso la tiene: la revision lee su entorno declarado.
    monkeypatch.setenv("SLACK_CONNECTOR_INTERNAL_HOST", INTERNAL_HOST)
    with pytest.raises(ValueError, match="^invalid_slack_connector_origin$"):
        DailyFeedbackRuntimeSettings.from_env(
            _instance_environment(SLACK_CONNECTOR_INTERNAL_HOST=internal_host)
        )


@pytest.mark.parametrize("internal_host", [None, "", "   "])
@pytest.mark.parametrize(
    ("environment", "exceptions"),
    [(_johanna_environment, ()), (_att1_environment, tuple(ATT1_EXCEPTIONS))],
)
def test_without_the_variable_johanna_and_att1_keep_todays_settings(
    environment, exceptions: tuple[str, ...], internal_host: str | None
) -> None:
    from bridge.daily_feedback_app import DailyFeedbackRuntimeSettings

    settings = DailyFeedbackRuntimeSettings.from_env(
        _with(environment(), {"SLACK_CONNECTOR_INTERNAL_HOST": internal_host})
    )

    assert settings.slack_connector_base_url == "https://connector.example.test"
    assert settings.slack_connector_internal_host == ""
    assert settings.exceptions == exceptions
    assert settings == DailyFeedbackRuntimeSettings.from_env(environment())


def test_declared_variable_with_an_https_connector_changes_nothing() -> None:
    from bridge.daily_feedback_app import DailyFeedbackRuntimeSettings

    settings = DailyFeedbackRuntimeSettings.from_env(
        _with(_att1_environment(), {"SLACK_CONNECTOR_INTERNAL_HOST": INTERNAL_HOST})
    )

    assert settings.slack_connector_base_url == "https://connector.example.test"
    assert settings.exceptions == tuple(ATT1_EXCEPTIONS)
