"""El flag AGENT_PRODUCTS_ENABLED en create_app (product-card-v1, E-569, MP-07).

Va con el cableado de ``src/bridge/app.py`` (el parche ``cableado-app-py.patch`` del kit de E-569), que espera a
que E-571 libere ``app.py``. Apagado (el default) las rutas no existen y la base no recibe ningun pedido.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from test_agent_product_routes import AE_KEY, ASKER, CHANGE, Db, _ae, _card
from test_flow_approval_page import ORIGIN, FakeChatwoot, _NoSupabase, _settings

from bridge import agent_product_api
from bridge import app as app_module
from bridge.app import Settings, create_app


def _app(*, enabled: bool) -> tuple[TestClient, Db]:
    db = Db()
    store = agent_product_api.ProductStore(base_url="https://att1-rest.internal", db_key=AE_KEY,
                                           transport=httpx.MockTransport(db.handler))
    settings = _settings(flow_approval_enabled=False, **CHANGE, agent_products_enabled=enabled)
    app = create_app(settings, supabase_client=_NoSupabase(),  # type: ignore[arg-type]
                     chatwoot_client=FakeChatwoot(), agent_product_store=store)  # type: ignore[arg-type]
    return TestClient(app, base_url=ORIGIN), db


def test_off_by_default() -> None:
    assert Settings.__dataclass_fields__["agent_products_enabled"].default is False
    assert "agent_products_enabled" in app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES


def test_with_the_flag_off_the_routes_do_not_exist_and_the_base_hears_nothing() -> None:
    client, db = _app(enabled=False)
    for method, path, body in (("POST", "/preview", {"product": _card()}), ("POST", "", {"product": _card()}),
                               ("GET", "", None), ("GET", f"/{uuid.uuid4()}", None),
                               ("POST", f"/{uuid.uuid4()}/validate", {"revision": 1, "requested_by": ASKER})):
        assert _ae(client, method, path, body).status_code in (404, 405)
    assert db.calls == []
    assert not [route for route in client.app.routes if "/products" in getattr(route, "path", "")]


def test_the_flag_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "CHATWOOT_WEBHOOK_SECRET": "test-secret",
        "ALLOWED_WHATSAPP_JID": "12025550123@s.whatsapp.net",
        "CHATWOOT_AGENT_BOT_ID": "1",
        "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
        "CHATWOOT_ACCOUNT_ID": "1",
        "CHATWOOT_CONTROL_API_ACCESS_TOKEN": "test-control-token",
        "CHATWOOT_PAUSE_MACRO_ID": "1",
        "CHATWOOT_INBOX_ID": "9",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(agent_product_api.AGENT_PRODUCTS_ENV, raising=False)
    assert Settings.from_env().agent_products_enabled is False
    monkeypatch.setenv(agent_product_api.AGENT_PRODUCTS_ENV, "true")
    assert Settings.from_env().agent_products_enabled is True


@pytest.mark.parametrize(("overrides", "message"), [
    ({"automation_expert_change_enabled": False}, "requires AUTOMATION_EXPERT_CHANGE_ENABLED"),
    ({"instance_manifest": None}, "requires an instance manifest"),
])
def test_the_flag_needs_the_change_routes_and_a_manifest(overrides: dict, message: str) -> None:
    settings = _settings(flow_approval_enabled=False, **{**CHANGE, **overrides}, agent_products_enabled=True)
    with pytest.raises(ValueError, match=message):
        agent_product_api.check_settings(settings)


def test_with_the_flag_on_create_app_registers_the_routes() -> None:
    client, db = _app(enabled=True)
    assert _ae(client, "POST", "/preview", {"product": _card()}).status_code == 200
    assert db.calls == ["list_agent_products_v1"]
