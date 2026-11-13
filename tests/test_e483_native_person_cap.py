"""E-483 (C6 del mapa «el AE maneja todos los flujos», 2026-11-12; D5): el tope por persona del nativo.

La base (20261112230000) cierra con ``tope_por_persona`` el toque nativo que pasaria el tope de la
instancia, contando los envios nativos de la persona en la ventana (lo mide
``validate_e483_native_person_cap.mjs`` en PGlite: A1 y A2 salen, Z cierra, la v1 sin tope lo deja pasar, el
seguimiento de una conversacion no cuenta, la otra forma del movil suma, y el rojo con las definiciones de antes).
Aca: el flag y los numeros en ``Settings``, el cliente (v2 con el tope, v1 sin el) y el despachador, que con el
flag apagado hace la llamada de hoy.

Los bordes: la fila ``terminado`` de ``tests/fixtures/flow_engine/bridge_rpc_rows.json`` (la RPC real en PGlite,
``validate_flow_engine_bridge_reads_v1.mjs --emit-bridge-fixtures``) con el motivo nuevo, y los dobles de la base y
de Chatwoot de ``test_flow_engine_native_dispatch.py``.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_flow_engine_native_dispatch import _dispatcher, _NativeAuthority, _NativeChatwoot, _run, _templates

import bridge.app as app_module
from bridge.app import Settings, _validate_native_person_cap, native_person_cap
from bridge.supabase import FlowEngineRunTerminated, NativePersonCap, SupabaseClient

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase" / "migrations" / "20261112230000_e483_native_person_cap_v1.sql"
ROWS = json.loads((ROOT / "tests" / "fixtures" / "flow_engine" / "bridge_rpc_rows.json").read_text())
FLAG = "FLOW_ENGINE_NATIVE_PERSON_CAP_ENABLED"
MINIMUM_ENV = {
    # El minimo para leer el entorno, el mismo de test_flow_engine_native_intent.
    "CHATWOOT_WEBHOOK_SECRET": "test-secret",
    "ALLOWED_WHATSAPP_JID": "12025550123@s.whatsapp.net",
    "CHATWOOT_AGENT_BOT_ID": "1",
    "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
    "CHATWOOT_ACCOUNT_ID": "1",
    "CHATWOOT_CONTROL_API_ACCESS_TOKEN": "test-control-token",
    "CHATWOOT_PAUSE_MACRO_ID": "1",
    "CHATWOOT_INBOX_ID": "9",
}


# -- El flag y los numeros ----------------------------------------------------------


def test_the_flag_is_off_by_default_portable_and_d5_is_the_default() -> None:
    fields = Settings.__dataclass_fields__
    assert fields["flow_engine_native_person_cap_enabled"].default is False
    assert fields["flow_engine_native_person_cap_max_sends"].default == 4
    assert fields["flow_engine_native_person_cap_window_hours"].default == 120
    assert "flow_engine_native_person_cap_enabled" in app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES


def test_from_env_reads_the_flag_and_the_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in MINIMUM_ENV.items():
        monkeypatch.setenv(name, value)
    settings = Settings.from_env()
    assert settings.flow_engine_native_person_cap_enabled is False
    assert native_person_cap(settings) is None
    monkeypatch.setenv(FLAG, "true")
    assert native_person_cap(Settings.from_env()) == NativePersonCap(max_sends=4, window_hours=120)
    monkeypatch.setenv("FLOW_ENGINE_NATIVE_PERSON_CAP_MAX_SENDS", "3")
    monkeypatch.setenv("FLOW_ENGINE_NATIVE_PERSON_CAP_WINDOW_HOURS", "72")
    assert native_person_cap(Settings.from_env()) == NativePersonCap(max_sends=3, window_hours=72)


@pytest.mark.parametrize(("field", "value", "message"), [
    ("flow_engine_native_person_cap_max_sends", 21, "FLOW_ENGINE_NATIVE_PERSON_CAP_MAX_SENDS must be 1-20"),
    ("flow_engine_native_person_cap_max_sends", 0, "FLOW_ENGINE_NATIVE_PERSON_CAP_MAX_SENDS must be 1-20"),
    ("flow_engine_native_person_cap_window_hours", 721, "FLOW_ENGINE_NATIVE_PERSON_CAP_WINDOW_HOURS must be 1-720"),
])
def test_a_number_the_base_would_refuse_stops_the_start_even_with_the_flag_off(
        monkeypatch: pytest.MonkeyPatch, field: str, value: int, message: str) -> None:
    for name, env_value in MINIMUM_ENV.items():
        monkeypatch.setenv(name, env_value)
    settings = Settings.from_env()
    object.__setattr__(settings, field, value)
    with pytest.raises(ValueError, match=message):
        _validate_native_person_cap(settings)


# -- El cliente ----------------------------------------------------------------------


def _start(person_cap: NativePersonCap | None, row: dict[str, Any]) -> tuple[Any, list[httpx.Request]]:
    requests: list[httpx.Request] = []
    clean = {key: value for key, value in row.items() if key != "_call"}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[clean])

    client = SupabaseClient(base_url="https://supabase.example.test", service_role_key="service-role",
                            transport=httpx.MockTransport(handler))
    call = ROWS["request_start"]["request_started"]["_call"]
    kwargs: dict[str, Any] = {} if person_cap is None else {"person_cap": person_cap}
    try:
        result: Any = asyncio.run(client.mark_flow_engine_request_started(
            action_id=call["action_id"], attempt_id=call["attempt_id"], worker_id="att1-dispatcher",
            lease_generation=call["lease_generation"], now="2026-10-22T15:00:00+00:00", **kwargs))
    except FlowEngineRunTerminated as exc:
        result = exc
    return result, requests


def test_without_the_cap_the_start_is_the_v1_of_today() -> None:
    attempt, [request] = _start(None, ROWS["request_start"]["request_started"])
    assert request.url.path == "/rest/v1/rpc/mark_flow_engine_request_started_v1"
    assert set(json.loads(request.content)) == {"p_action_id", "p_attempt_id", "p_worker_id", "p_lease_generation",
                                                "p_now"}
    assert attempt.phase == "request_started"


def test_with_the_cap_the_start_is_the_v2_with_the_numbers_of_the_instance() -> None:
    attempt, [request] = _start(NativePersonCap(max_sends=4, window_hours=120), ROWS["request_start"]["request_started"])
    assert request.url.path == "/rest/v1/rpc/mark_flow_engine_request_started_v2"
    assert json.loads(request.content)["p_person_cap"] == {"max_sends": 4, "window_hours": 120}
    assert attempt.phase == "request_started"


def test_a_step_over_the_cap_ends_terminated_with_its_reason() -> None:
    # La fila terminado capturada, con el motivo que deja la base nueva (validate_e483_native_person_cap.mjs).
    row = {**copy.deepcopy(ROWS["request_start"]["terminado"]), "reason": "tope_por_persona"}
    result, [request] = _start(NativePersonCap(max_sends=4, window_hours=120), row)
    assert request.url.path == "/rest/v1/rpc/mark_flow_engine_request_started_v2"
    assert isinstance(result, FlowEngineRunTerminated) and result.reason == "tope_por_persona"


# -- El despachador ------------------------------------------------------------------


class _CappedAuthority(_NativeAuthority):
    """La base que cierra el toque por el tope, y que anota con que se lo pidieron."""

    def __init__(self, *, over_cap: bool) -> None:
        super().__init__()
        self.over_cap = over_cap
        self.start_kwargs: list[dict[str, object]] = []

    async def mark_flow_engine_request_started(self, **kwargs: object) -> Any:
        self.start_kwargs.append(kwargs)
        if self.over_cap:
            self.engine_calls.append("request_started")
            raise FlowEngineRunTerminated("tope_por_persona")
        return await super().mark_flow_engine_request_started(**kwargs)


def _native(authority: _CappedAuthority, chatwoot: _NativeChatwoot, tmp_path: Path,
            cap: NativePersonCap | None) -> Any:
    dispatcher = _dispatcher(authority, chatwoot, tmp_path)
    dispatcher._native_person_cap = cap
    return dispatcher


def test_without_the_flag_the_dispatcher_makes_the_call_of_today(tmp_path: Path) -> None:
    authority = _CappedAuthority(over_cap=False)
    chatwoot = _NativeChatwoot()
    _run(_native(authority, chatwoot, tmp_path, None))
    [kwargs] = authority.start_kwargs
    assert "person_cap" not in kwargs
    assert len(_templates(chatwoot)) == 1


def test_with_the_flag_the_dispatcher_passes_the_cap(tmp_path: Path) -> None:
    authority = _CappedAuthority(over_cap=False)
    chatwoot = _NativeChatwoot()
    _run(_native(authority, chatwoot, tmp_path, NativePersonCap(max_sends=4, window_hours=120)))
    [kwargs] = authority.start_kwargs
    assert kwargs["person_cap"] == NativePersonCap(max_sends=4, window_hours=120)
    assert len(_templates(chatwoot)) == 1


def test_a_step_over_the_cap_sends_nothing_and_the_base_already_closed_it(tmp_path: Path) -> None:
    authority = _CappedAuthority(over_cap=True)
    chatwoot = _NativeChatwoot()
    _run(_native(authority, chatwoot, tmp_path, NativePersonCap(max_sends=4, window_hours=120)))
    assert _templates(chatwoot) == []
    # La base ya cerro el intento: el despachador no lo finaliza otra vez.
    assert authority.finalizations == []


# -- La migracion y su prueba -----------------------------------------------------------


def test_the_migration_follows_the_mold() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert sql.count("\nbegin;\n") == 1 and sql.rstrip().endswith("commit;")
    assert "set local lock_timeout = '5s';" in sql
    assert "message = 'e483_native_person_cap_already_applied'" in sql
    for needed in ("tope_por_persona", "flow_engine.native_person_cap", "_whatsapp_phone_variants",
                   "flow-engine-native-person-cap:", "join public.flow_engine_native_runs cap_native",
                   "grant execute on function public.mark_flow_engine_request_started_v2"):
        assert needed in sql, needed
    # No toca el tope del legado.
    assert "insert into public.pilot_proactive_contact_caps" not in sql


def test_the_new_test_runs_with_npm_test() -> None:
    package = json.loads((ROOT / "tests/sql/followup_engine/package.json").read_text(encoding="utf-8"))
    assert package["scripts"]["test"].endswith(" && node validate_e483_native_person_cap.mjs && node validate_e528_native_payment_slip.mjs")
