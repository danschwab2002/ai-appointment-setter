"""El correlador que decide solo en el bridge (E-536): los flags, las guardas de
arranque, /ready y el destino del carrito atado en resolution.py.

Sobre la app de ATT1 de ``test_flow_engine_bridge_settings`` y la base falsa de
``test_flow_engine_ready`` (sin lifespan: los workers no arrancan).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from test_flow_engine_bridge_settings import engine_settings
from test_flow_engine_ready import _app, _ready, _ReadyAuthority

from bridge import app as app_module
from bridge import resolution
from bridge.app import Settings, _identity_link_ask_window
from bridge.supabase import SupabaseError

MODEL = {
    "hermes_api_base_url": "http://hermes:8642/v1",
    "hermes_api_key": "clave-de-prueba",
    "correlation_preresolution_model_name": "modelo/prueba",
    "identity_link_worker_id": "worker-prueba",
}


def test_the_flags_are_off_by_default_and_portable() -> None:
    fields = Settings.__dataclass_fields__
    assert fields["identity_link_mode"].default == "off"
    assert fields["identity_link_model_only"].default is False
    assert fields["identity_link_country_code_fix_enabled"].default is False
    assert fields["identity_link_ask_enabled"].default is False
    for name in ("identity_link_model_only", "identity_link_country_code_fix_enabled", "identity_link_ask_enabled"):
        assert name in app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES


def test_off_the_ready_payload_does_not_change(tmp_path: Path) -> None:
    response = _ready(_app(tmp_path, _ReadyAuthority()))
    assert response.status_code == 200
    assert not any(key.startswith("identity_link") for key in response.json())


@pytest.mark.parametrize("mode", ["sombra", "vivo"])
def test_on_ready_says_the_mode_and_that_it_has_no_slack(tmp_path: Path, mode: str) -> None:
    app = _app(tmp_path, _ReadyAuthority(), identity_link_mode=mode, **MODEL)
    body = _ready(app).json()
    assert body["identity_link"] == mode
    assert body["identity_link_ask"] == "disabled"
    assert body["identity_link_alerts"] == "sin_slack"
    assert app.state.identity_link_worker.mode == mode


@pytest.mark.parametrize("missing", sorted(MODEL))
def test_on_without_the_model_connection_it_does_not_start(tmp_path: Path, missing: str) -> None:
    with pytest.raises(ValueError, match="identity_link_configuration_incomplete"):
        _app(tmp_path, _ReadyAuthority(), identity_link_mode="vivo", **{**MODEL, missing: None})


def test_ask_without_the_template_in_the_manifest_does_not_start(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"\[plantillas\]\.identidad"):
        _app(tmp_path, _ReadyAuthority(), identity_link_mode="vivo", identity_link_ask_enabled=True, **MODEL)


def test_ask_with_the_template_in_the_manifest_starts(tmp_path: Path) -> None:
    from dataclasses import replace
    from types import MappingProxyType

    from bridge.instance_manifest import Template

    settings = engine_settings(tmp_path, identity_link_mode="vivo", identity_link_ask_enabled=True, **MODEL)
    manifest = settings.instance_manifest
    assert manifest is not None
    templates = MappingProxyType({**manifest.templates,
                                  "identidad": Template(name="aliada_compra_iniciada_01", language="es_MX")})
    settings = replace(settings, instance_manifest=replace(manifest, templates=templates))
    app = app_module.create_app(
        settings, supabase_client=_ReadyAuthority(),  # type: ignore[arg-type]
        chatwoot_client=__import__("test_cart_discount_first_touch")._Chatwoot().client(),
    )
    assert _ready(app).json()["identity_link_ask"] == "enabled"


@pytest.mark.parametrize(("value", "ok"), [
    ("09:00-21:00", True), ("08:30-20:15", True), ("21:00-09:00", False), ("9-21", False), ("25:00-26:00", False),
])
def test_the_ask_window(value: str, ok: bool) -> None:
    if ok:
        start, end = _identity_link_ask_window(value)
        assert start < end
    else:
        with pytest.raises(ValueError):
            _identity_link_ask_window(value)


def test_vivo_ties_the_cart_planning_to_the_phone_of_the_tie(tmp_path: Path) -> None:
    on = _app(tmp_path, _ReadyAuthority(), identity_link_mode="vivo", **MODEL)
    assert on.state.resolution_worker._identity_link_bind is True
    shadow = _app(tmp_path, _ReadyAuthority(), identity_link_mode="sombra", **MODEL)
    assert shadow.state.resolution_worker._identity_link_bind is False


class _Bind:
    """La base de bind_identity_link_contact_v1, por la RPC de PostgREST."""

    def __init__(self, answer: object, *, fail: bool = False) -> None:
        self.answer = answer
        self.fail = fail
        self.bodies: list[str] = []
        self.identity_reads = 0

    async def _request(self, method: str, path: str, *, content: str | None = None, **_: Any) -> Any:
        import httpx

        assert path == "/rest/v1/rpc/bind_identity_link_contact_v1"
        self.bodies.append(content or "")
        if self.fail:
            raise SupabaseError("supabase_request_failed: POST")
        return httpx.Response(200, json=self.answer)

    async def find_active_whatsapp_identities(self, **_: Any) -> list[object]:
        self.identity_reads += 1
        return []


@pytest.mark.parametrize(("answer", "expected"), [
    ("525512345678", "525512345678"),   # la atadura: el telefono del permiso
    (None, "525599999999"),             # sin atadura: el de hoy
])
def test_the_cart_writes_to_the_phone_the_base_says(answer: object, expected: str) -> None:
    base = _Bind(answer)
    phone = asyncio.run(resolution._identity_link_destination(
        base,  # type: ignore[arg-type]
        webhook_event_id="11111111-1111-4111-8111-111111111111", contact_id="22222222-2222-4222-8222-222222222222",
        external_user_id="525599999999", chatwoot_account_id=1, chatwoot_inbox_id=13,
    ))
    assert phone == expected
    assert '"p_webhook_event_id": "11111111-1111-4111-8111-111111111111"' in base.bodies[0]


def test_a_failed_read_keeps_todays_phone() -> None:
    phone = asyncio.run(resolution._identity_link_destination(
        _Bind(None, fail=True),  # type: ignore[arg-type]
        webhook_event_id="11111111-1111-4111-8111-111111111111", contact_id="22222222-2222-4222-8222-222222222222",
        external_user_id="525599999999", chatwoot_account_id=1, chatwoot_inbox_id=13,
    ))
    assert phone == "525599999999"
