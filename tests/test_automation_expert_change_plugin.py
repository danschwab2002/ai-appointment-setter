"""El plugin de cambios del Automation Expert (flow-change-cycle-v1, «Las rutas y las herramientas»).

El cliente se prueba contra un bridge falso de verdad: un servidor HTTP local que contesta con la
forma de las rutas de src/bridge/flow_change_api.py y anota lo que recibe. Fuera de Hermes, el
contexto de la sesion sale del entorno (HERMES_SESSION_PLATFORM y HERMES_SESSION_USER_ID).
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace
from typing import Any, Iterator

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "profiles" / "automation-expert" / "plugins" / "automation-expert-change"
TOKEN = "c" * 40
PROPOSAL = "5d0c2b9e-1f3a-4b6c-8d7e-9f0a1b2c3d4e"
FLOW = "00000001-0000-4000-8000-0000000a7701"
PREFIX = "/internal/automation-expert/v1"
TOOLS = ["get_flow_definitions", "propose_flow_change", "validate_flow_change", "get_flow_change"]


def _load_plugin() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "automation_expert_change_plugin", PLUGIN / "__init__.py",
        submodule_search_locations=[str(PLUGIN)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _tools() -> ModuleType:
    return sys.modules[_load_plugin().__name__ + ".tools"]


def test_the_plugin_registers_its_four_tools_in_its_toolset_and_guards_the_profile() -> None:
    plugin = _load_plugin()
    tools: list[tuple[str, str]] = []
    hooks: dict[str, Any] = {}
    plugin.register(SimpleNamespace(
        profile_name="automation-expert",
        register_tool=lambda **kwargs: tools.append((kwargs["name"], kwargs["toolset"])),
        register_hook=lambda name, callback: hooks.__setitem__(name, callback),
    ))
    assert tools == [(name, "automation_expert_change") for name in TOOLS]
    assert hooks["pre_tool_call"](tool_name="propose_flow_change") is None
    other: dict[str, Any] = {}
    plugin.register(SimpleNamespace(profile_name="agente-comercial", register_tool=lambda **kwargs: None,
                                    register_hook=lambda name, callback: other.__setitem__(name, callback)))
    for name in TOOLS:
        assert other["pre_tool_call"](tool_name=name)["action"] == "block"
    assert other["pre_tool_call"](tool_name="list_flows") is None


def test_no_tool_approves_publishes_or_pauses() -> None:
    schemas = sys.modules[_load_plugin().__name__ + ".schemas"]
    names = {value["name"] for value in vars(schemas).values() if isinstance(value, dict) and "name" in value}
    assert names == set(TOOLS)
    assert not any(word in name for name in names for word in ("approve", "publish", "pause", "discard"))


class _Bridge(BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, object]] = {}
    seen: list[dict[str, object]] = []

    def log_message(self, *args: object) -> None:  # noqa: D401 - silencio
        return

    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8") if length else ""
        type(self).seen.append({"method": self.command, "path": self.path, "body": body,
                                "authorization": self.headers.get("Authorization")})
        status, payload = type(self).routes.get(f"{self.command} {self.path}",
                                                (404, {"detail": "flow_change_not_found"}))
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = _answer
    do_POST = _answer


@pytest.fixture()
def bridge(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[_Bridge]]:
    _Bridge.routes = {}
    _Bridge.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Bridge)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host = f"127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("AUTOMATION_EXPERT_API_URL", f"http://{host}")
    monkeypatch.setenv("AUTOMATION_EXPERT_INTERNAL_HOST", host)
    monkeypatch.setenv("AUTOMATION_EXPERT_CHANGE_API_TOKEN", TOKEN)
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "slack")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "U0123456789")
    try:
        yield _Bridge
    finally:
        server.shutdown()


def _envelope(data: object, version: str = "1.3.0") -> dict[str, object]:
    return {"contract": "flow-change-cycle-v1", "contract_version": version, "data": data}


def _item() -> dict[str, object]:
    return {"producto": "att1", "flujo": "carrito", "flow_id": FLOW, "tipo": "version",
            "definicion": {"flujo": "carrito"}, "en_curso": "seguir"}


def test_the_definitions_go_through_with_the_change_token(bridge: type[_Bridge]) -> None:
    bridge.routes[f"GET {PREFIX}/flow-store"] = (200, _envelope({"flows": [{"flow_id": FLOW}]}))
    result = json.loads(_tools().get_flow_definitions({}))
    assert result["data"]["flows"] == [{"flow_id": FLOW}]
    assert bridge.seen[0]["authorization"] == f"Bearer {TOKEN}"


def test_who_asks_comes_from_the_slack_session_never_from_the_model(bridge: type[_Bridge]) -> None:
    bridge.routes[f"POST {PREFIX}/flow-changes"] = (201, _envelope({"proposal_id": PROPOSAL, "revision": 1}))
    params = {"items": [_item()], "base": {FLOW: {"active_version": 1}}, "purpose": " A2 a las 2 h ",
              "requested_by": {"slack_user": "UOTRAPERSONA"}}
    result = json.loads(_tools().propose_flow_change(params))
    assert result["data"]["proposal_id"] == PROPOSAL
    sent = json.loads(str(bridge.seen[0]["body"]))
    assert sent["requested_by"] == {"slack_user": "U0123456789"}
    assert sent["purpose"] == "A2 a las 2 h"
    assert set(sent) == {"items", "base", "purpose", "requested_by"}


@pytest.mark.parametrize("platform,user", [("cli", "U0123456789"), ("slack", ""), ("slack", "dan")])
def test_outside_a_slack_session_nothing_is_proposed(bridge: type[_Bridge], monkeypatch: pytest.MonkeyPatch,
                                                     platform: str, user: str) -> None:
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", platform)
    monkeypatch.setenv("HERMES_SESSION_USER_ID", user)
    tools = _tools()
    params = {"items": [_item()], "base": {}, "purpose": "x"}
    assert json.loads(tools.propose_flow_change(params)) == {"error": "requested_by_unavailable"}
    assert json.loads(tools.validate_flow_change({"proposal_id": PROPOSAL, "revision": 1})) == {
        "error": "requested_by_unavailable"}
    assert bridge.seen == []


@pytest.mark.parametrize("params,error", [
    ({"items": [], "base": {}, "purpose": "que le escriba al 55 1234 5678"}, "identifier_not_allowed"),
    ({"items": [], "base": {}, "purpose": "avisarle a ana@example.test"}, "identifier_not_allowed"),
    ({"items": [], "base": {}, "purpose": "  "}, "invalid_body"),
    ({"items": [], "base": {}, "purpose": "x", "proposal_id": PROPOSAL}, "invalid_body"),
    ({"items": [], "base": {}, "purpose": "x", "proposal_id": "no-es-uuid", "revision": 1}, "invalid_body"),
])
def test_bad_proposals_never_reach_the_network(bridge: type[_Bridge], params: dict[str, object], error: str) -> None:
    assert json.loads(_tools().propose_flow_change(params)) == {"error": error}
    assert bridge.seen == []


def test_a_blocked_draft_comes_back_with_its_blocks(bridge: type[_Bridge]) -> None:
    blocks = [{"code": "flow_change_stale_base", "flow_id": FLOW}]
    bridge.routes[f"POST {PREFIX}/flow-changes"] = (
        422, {"detail": "flow_change_draft_blocked", "data": {"blocks": blocks, "warnings": []}})
    result = json.loads(_tools().propose_flow_change({"items": [_item()], "base": {}, "purpose": "x"}))
    assert result == {"error": "flow_change_draft_blocked", "blocks": blocks, "warnings": []}


def test_validate_and_read_send_only_what_they_need(bridge: type[_Bridge]) -> None:
    bridge.routes[f"POST {PREFIX}/flow-changes/{PROPOSAL}/validate"] = (200, _envelope({"preview": {}}))
    bridge.routes[f"GET {PREFIX}/flow-changes/{PROPOSAL}"] = (200, _envelope({"status": "validada"}))
    tools = _tools()
    assert json.loads(tools.validate_flow_change({"proposal_id": PROPOSAL, "revision": 2}))["data"] == {"preview": {}}
    assert json.loads(str(bridge.seen[0]["body"])) == {"revision": 2, "requested_by": {"slack_user": "U0123456789"}}
    assert json.loads(tools.get_flow_change({"proposal_id": PROPOSAL}))["data"] == {"status": "validada"}
    assert json.loads(tools.get_flow_change({"proposal_id": "x"})) == {"error": "invalid_proposal_id"}
    assert json.loads(tools.validate_flow_change({"proposal_id": PROPOSAL, "revision": "2"})) == {"error": "invalid_body"}


def test_errors_map_to_closed_codes(bridge: type[_Bridge], monkeypatch: pytest.MonkeyPatch) -> None:
    tools = _tools()
    route = f"GET {PREFIX}/flow-changes/{PROPOSAL}"
    bridge.routes[route] = (409, {"detail": "flow_change_revision_mismatch"})
    assert json.loads(tools.get_flow_change({"proposal_id": PROPOSAL})) == {"error": "flow_change_revision_mismatch"}
    # Un detalle que no es un codigo cerrado no pasa tal cual.
    bridge.routes[route] = (500, {"detail": "Traceback 5512345678"})
    assert json.loads(tools.get_flow_change({"proposal_id": PROPOSAL})) == {"error": "automation_expert_change_unavailable"}
    bridge.routes[route] = (401, {"detail": "otra cosa"})
    assert json.loads(tools.get_flow_change({"proposal_id": PROPOSAL})) == {
        "error": "automation_expert_change_authentication_required"}
    bridge.routes[route] = (200, _envelope({}, version="2.0.0"))
    assert json.loads(tools.get_flow_change({"proposal_id": PROPOSAL})) == {
        "error": "automation_expert_change_contract_unsupported"}
    # Sin su propia clave, o con la de lectura sola, no sale nada.
    monkeypatch.setenv("AUTOMATION_EXPERT_CHANGE_API_TOKEN", "corta")
    seen = len(bridge.seen)
    assert json.loads(tools.get_flow_definitions({})) == {"error": "automation_expert_change_api_token_invalid"}
    assert len(bridge.seen) == seen
