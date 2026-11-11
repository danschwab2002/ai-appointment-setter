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
TOOLS = ["get_flow_definitions", "propose_flow_change", "validate_flow_change", "get_flow_change",
         # Ciclo 1.10.0 (E-208): las dos lecturas del catalogo y de las propuestas.
         "list_flow_changes", "list_templates",
         # template-request-v1 (E-356, F-146 parte 5): guardan y validan, ninguna manda a Meta.
         "propose_template", "validate_template", "list_template_requests", "get_template_request",
         # automation-expert-chatwoot-v1 (E-256, F-143): la herramienta acotada de Chatwoot.
         "list_chatwoot_labels", "create_chatwoot_label", "add_chatwoot_label_to_conversation",
         "list_chatwoot_macros",
         # agent-campaigns-v1 (E-355): armar una campana. Publicarla es de la pagina.
         "create_chatwoot_label_macro", "preview_campaign", "propose_campaign", "validate_campaign",
         "get_campaign", "list_campaigns"]


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


def test_the_plugin_registers_its_tools_in_its_toolset_and_guards_the_profile() -> None:
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
    # Ninguna borra, saca, ejecuta ni manda (F-143: lectura y creacion si, borrado no).
    assert not any(word in name for name in names for word in ("delete", "remove", "execute", "send"))


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


def test_a_request_that_does_not_close_comes_back_with_its_reason(bridge: type[_Bridge]) -> None:
    # La forma que contesta el bridge desde el 22/10 (flow-change-cycle-v1 1.6.0).
    reason = {"field": "items", "code": "keys_invalid", "item": 0, "missing": ["flujo"], "extra": ["flow_key"]}
    bridge.routes[f"POST {PREFIX}/flow-changes"] = (422, {"detail": "flow_change_items_invalid", "reason": reason})
    result = json.loads(_tools().propose_flow_change({"items": [_item()], "base": {}, "purpose": "x"}))
    assert result == {"error": "flow_change_items_invalid", "reason": reason}
    # Lo que no tiene la forma cerrada no pasa: un texto largo, un numero, una clave con espacios.
    bridge.routes[f"POST {PREFIX}/flow-changes"] = (422, {"detail": "invalid_body", "reason": {
        "field": "requested_by por Ana 5512345678", "item": "0", "extra": ["Ana 5512345678", "version"]}})
    result = json.loads(_tools().propose_flow_change({"items": [_item()], "base": {}, "purpose": "x"}))
    assert result == {"error": "invalid_body", "reason": {"extra": ["version"]}}
    bridge.routes[f"POST {PREFIX}/flow-changes"] = (422, {"detail": "invalid_body", "reason": "texto libre"})
    assert json.loads(_tools().propose_flow_change({"items": [_item()], "base": {}, "purpose": "x"})) == {
        "error": "invalid_body"}


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


# ------------------------------------------------------------------- ciclo 1.10.0 (E-208, M16 P6 y P7)


def test_list_flow_changes_asks_open_ones_by_default_and_validates_before_the_network(
        bridge: type[_Bridge]) -> None:
    bridge.routes[f"GET {PREFIX}/flow-changes?estado=abiertas&limit=20"] = (
        200, _envelope({"estado": "abiertas", "proposals": [{"proposal_id": PROPOSAL}], "open": 1}))
    bridge.routes[f"GET {PREFIX}/flow-changes?estado=todas&limit=50"] = (
        200, _envelope({"estado": "todas", "proposals": []}))
    tools = _tools()
    assert json.loads(tools.list_flow_changes({}))["data"]["proposals"] == [{"proposal_id": PROPOSAL}]
    assert json.loads(tools.list_flow_changes({"estado": "todas", "limit": 50}))["data"]["estado"] == "todas"
    assert bridge.seen[0]["authorization"] == f"Bearer {TOKEN}" and bridge.seen[0]["method"] == "GET"
    seen = len(bridge.seen)
    for params in ({"estado": "cerradas"}, {"limit": 0}, {"limit": 51}, {"limit": "5"}, {"limit": True}):
        assert json.loads(tools.list_flow_changes(params)) == {"error": "invalid_query"}
    assert len(bridge.seen) == seen
    bridge.routes[f"GET {PREFIX}/flow-changes?estado=abiertas&limit=20"] = (422, {"detail": "invalid_query"})
    assert json.loads(tools.list_flow_changes({})) == {"error": "invalid_query"}


def test_list_templates_reads_the_catalog_route(bridge: type[_Bridge]) -> None:
    bridge.routes[f"GET {PREFIX}/templates"] = (200, _envelope({"templates": [{"nombre": "x"}], "total": 1}))
    result = json.loads(_tools().list_templates({}))
    assert result["data"]["templates"] == [{"nombre": "x"}]
    assert bridge.seen[0]["path"] == f"{PREFIX}/templates"
    bridge.routes[f"GET {PREFIX}/templates"] = (503, {"detail": "template_catalog_unavailable"})
    assert json.loads(_tools().list_templates({})) == {"error": "template_catalog_unavailable"}


# ----------------------------------------------------------------- Chatwoot (automation-expert-chatwoot-v1)


CHATWOOT = f"{PREFIX}/chatwoot"


def _cw(data: object) -> dict[str, object]:
    return {"contract": "automation-expert-chatwoot-v1", "contract_version": "1.0.0", "data": data}


def test_the_chatwoot_reads_go_to_their_routes_with_the_change_token(bridge: type[_Bridge]) -> None:
    bridge.routes[f"GET {CHATWOOT}/labels"] = (200, _cw({"labels": [{"title": "cliente"}]}))
    bridge.routes[f"GET {CHATWOOT}/macros"] = (200, _cw({"macros": [{"id": 12}]}))
    tools = _tools()
    assert json.loads(tools.list_chatwoot_labels({}))["data"]["labels"] == [{"title": "cliente"}]
    assert json.loads(tools.list_chatwoot_macros({}))["data"]["macros"] == [{"id": 12}]
    assert [seen["path"] for seen in bridge.seen] == [f"{CHATWOOT}/labels", f"{CHATWOOT}/macros"]
    assert all(seen["authorization"] == f"Bearer {TOKEN}" for seen in bridge.seen)


def test_creating_a_label_sends_who_asks_from_the_session(bridge: type[_Bridge]) -> None:
    bridge.routes[f"POST {CHATWOOT}/labels"] = (201, _cw({"created": True, "label": {"title": "cliente"}}))
    result = json.loads(_tools().create_chatwoot_label({"title": " Cliente ", "description": "Compro",
                                                         "color": "#1f93ff"}))
    assert result["data"]["created"] is True
    assert json.loads(str(bridge.seen[0]["body"])) == {"title": "cliente", "description": "Compro",
                                                       "color": "#1f93ff", "requested_by": {"slack_user": "U0123456789"}}


def test_adding_a_label_goes_to_the_conversation_route(bridge: type[_Bridge]) -> None:
    bridge.routes[f"POST {CHATWOOT}/conversations/50/labels"] = (200, _cw({"changed": True}))
    result = json.loads(_tools().add_chatwoot_label_to_conversation({"conversation_id": 50, "label": "cliente"}))
    assert result["data"]["changed"] is True
    assert json.loads(str(bridge.seen[0]["body"])) == {"label": "cliente",
                                                       "requested_by": {"slack_user": "U0123456789"}}


@pytest.mark.parametrize(("tool", "params", "code"), [
    ("create_chatwoot_label", {"title": "automation_paused"}, "chatwoot_label_reserved"),
    ("create_chatwoot_label", {"title": "con espacio"}, "invalid_body"),
    ("create_chatwoot_label", {"title": 5}, "invalid_body"),
    ("create_chatwoot_label", {"title": "cliente", "description": "a ana@example.org"}, "identifier_not_allowed"),
    ("add_chatwoot_label_to_conversation", {"conversation_id": "50", "label": "cliente"}, "invalid_body"),
    ("add_chatwoot_label_to_conversation", {"conversation_id": 0, "label": "cliente"}, "invalid_body"),
    ("add_chatwoot_label_to_conversation", {"conversation_id": True, "label": "cliente"}, "invalid_body"),
    ("add_chatwoot_label_to_conversation", {"conversation_id": 50, "label": "automation_opted_out"},
     "chatwoot_label_reserved"),
])
def test_the_chatwoot_writes_check_before_the_network(bridge: type[_Bridge], tool: str, params: dict,
                                                      code: str) -> None:
    assert json.loads(getattr(_tools(), tool)(params)) == {"error": code}
    assert bridge.seen == []


def test_outside_slack_the_chatwoot_writes_do_not_go(bridge: type[_Bridge], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "cli")
    tools = _tools()
    assert json.loads(tools.create_chatwoot_label({"title": "cliente"})) == {"error": "requested_by_unavailable"}
    assert json.loads(tools.add_chatwoot_label_to_conversation({"conversation_id": 50, "label": "cliente"})) == {
        "error": "requested_by_unavailable"}
    assert bridge.seen == []


def test_a_bridge_without_the_flag_says_the_tool_is_not_on(bridge: type[_Bridge]) -> None:
    # Sin AUTOMATION_EXPERT_CHATWOOT_ENABLED la ruta no existe: FastAPI contesta su 404 generico.
    bridge.routes[f"GET {CHATWOOT}/labels"] = (404, {"detail": "Not Found"})
    assert json.loads(_tools().list_chatwoot_labels({})) == {"error": "chatwoot_tool_not_enabled"}
    # El mismo 404 en una ruta de cambio sigue siendo «no disponible», como antes.
    bridge.routes[f"GET {PREFIX}/flow-store"] = (404, {"detail": "Not Found"})
    assert json.loads(_tools().get_flow_definitions({})) == {"error": "automation_expert_change_unavailable"}


@pytest.mark.parametrize("code", ["chatwoot_delete_not_allowed", "chatwoot_call_not_allowed",
                                  "chatwoot_conversation_not_found", "chatwoot_label_not_found",
                                  "chatwoot_tool_unavailable"])
def test_the_closed_codes_of_the_chatwoot_routes_come_back_as_they_are(bridge: type[_Bridge], code: str) -> None:
    bridge.routes[f"POST {CHATWOOT}/conversations/50/labels"] = (404, {"detail": code})
    result = json.loads(_tools().add_chatwoot_label_to_conversation({"conversation_id": 50, "label": "cliente"}))
    assert result == {"error": code}


# ----------------------------------------------------------------- plantillas (template-request-v1, F-146 parte 5)


REQUEST = "7a1c2b9e-1f3a-4b6c-8d7e-9f0a1b2c3d4f"


def _tpl(data: object) -> dict[str, object]:
    return {"contract": "template-request-v1", "contract_version": "1.1.0", "data": data}


def _template() -> dict[str, object]:
    return {"nombre": "att1_comunidad_01", "idioma": "es_MX", "categoria": "UTILITY",
            "cuerpo": "Hola {{nombre}}, te esperamos en la comunidad de {{producto}}. Entrá con el botón."}


def test_proposing_a_template_sends_who_asks_from_the_session_and_its_flow(bridge: type[_Bridge]) -> None:
    bridge.routes[f"POST {PREFIX}/template-requests"] = (201, _tpl({"request_id": REQUEST, "revision": 1}))
    for_flow = {"proposal_id": PROPOSAL, "step": "/items/0/definicion/pasos/0"}
    out = json.loads(_tools().propose_template({"template": _template(), "purpose": " la comunidad ", "for_flow": for_flow}))
    assert out["data"]["request_id"] == REQUEST
    sent = json.loads(bridge.seen[0]["body"])
    assert sent == {"template": _template(), "purpose": "la comunidad", "for_flow": for_flow,
                    "requested_by": {"slack_user": "U0123456789"}}
    assert bridge.seen[0]["authorization"] == f"Bearer {TOKEN}"


def test_proposing_a_template_keeps_the_slack_thread_of_this_conversation(
    bridge: type[_Bridge], monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge.routes[f"POST {PREFIX}/template-requests"] = (201, _tpl({"request_id": REQUEST, "revision": 1}))
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "D0123456789")
    monkeypatch.setenv("HERMES_SESSION_THREAD_ID", "1762700000.123456")
    # Lo que diga el modelo no cuenta: sale de la sesion.
    _tools().propose_template({"template": _template(), "purpose": "x", "slack_thread": {"channel": "C0OTROCANAL"}})
    assert json.loads(bridge.seen[0]["body"])["slack_thread"] == {"channel": "D0123456789",
                                                                  "thread_ts": "1762700000.123456"}


@pytest.mark.parametrize(("chat", "thread", "expected"), [
    ("D0123456789", "", {"channel": "D0123456789", "thread_ts": None}),
    ("D0123456789", "no-es-un-ts", {"channel": "D0123456789", "thread_ts": None}),
    ("", "1762700000.123456", None),
    ("no-es-canal", "", None),
])
def test_a_session_without_a_valid_channel_sends_no_thread(
    bridge: type[_Bridge], monkeypatch: pytest.MonkeyPatch, chat: str, thread: str, expected: object,
) -> None:
    bridge.routes[f"POST {PREFIX}/template-requests"] = (201, _tpl({"request_id": REQUEST, "revision": 1}))
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", chat)
    monkeypatch.setenv("HERMES_SESSION_THREAD_ID", thread)
    _tools().propose_template({"template": _template(), "purpose": "x"})
    assert json.loads(bridge.seen[0]["body"]).get("slack_thread") == expected


def test_redoing_a_template_draft_needs_its_id_and_revision(bridge: type[_Bridge]) -> None:
    bridge.routes[f"POST {PREFIX}/template-requests"] = (201, _tpl({"request_id": REQUEST, "revision": 2}))
    _tools().propose_template({"template": _template(), "purpose": "x", "request_id": REQUEST, "revision": 1})
    sent = json.loads(bridge.seen[0]["body"])
    assert (sent["request_id"], sent["revision"]) == (REQUEST, 1)
    bridge.seen.clear()
    assert json.loads(_tools().propose_template({"template": _template(), "purpose": "x", "revision": 1})) == {
        "error": "invalid_body"}
    assert bridge.seen == []


@pytest.mark.parametrize(("params", "error"), [
    ({"template": {}}, "invalid_body"),
    ({"template": {}, "purpose": "  "}, "invalid_body"),
    ({"template": {}, "purpose": "pedido de ana@mail.com"}, "identifier_not_allowed"),
])
def test_a_bad_template_purpose_never_reaches_the_network(bridge: type[_Bridge], params: dict[str, object],
                                                          error: str) -> None:
    assert json.loads(_tools().propose_template(params)) == {"error": error}
    assert bridge.seen == []


def test_a_blocked_template_comes_back_with_its_blocks(bridge: type[_Bridge]) -> None:
    blocks = [{"code": "template_marketing_without_opt_out", "path": "/botones", "reason": "..."}]
    bridge.routes[f"POST {PREFIX}/template-requests"] = (422, {
        "detail": "template_request_draft_blocked", **_tpl({"blocks": blocks, "warnings": []})})
    out = json.loads(_tools().propose_template({"template": _template(), "purpose": "x"}))
    assert out == {"error": "template_request_draft_blocked", "blocks": blocks, "warnings": []}


def test_a_template_that_does_not_close_says_which_field(bridge: type[_Bridge]) -> None:
    bridge.routes[f"POST {PREFIX}/template-requests"] = (422, {
        "detail": "template_request_invalid", "reason": {"field": "template.botones.0", "problem": "keys"}})
    out = json.loads(_tools().propose_template({"template": _template(), "purpose": "x"}))
    assert out == {"error": "template_request_invalid", "reason": {"field": "template.botones.0", "problem": "keys"}}


def test_without_the_flag_the_template_tools_say_it_is_not_on(bridge: type[_Bridge]) -> None:
    # Sin META_TEMPLATES_ENABLED las rutas no existen: el 404 generico de FastAPI.
    bridge.routes[f"GET {PREFIX}/template-requests?estado=todas&limit=20"] = (404, {"detail": "Not Found"})
    assert json.loads(_tools().list_template_requests({"estado": "todas"})) == {
        "error": "template_requests_not_enabled"}
    bridge.routes[f"POST {PREFIX}/template-requests"] = (404, {"detail": "Not Found"})
    assert json.loads(_tools().propose_template({"template": _template(), "purpose": "x"})) == {
        "error": "template_requests_not_enabled"}


def test_the_template_reads_check_before_the_network(bridge: type[_Bridge]) -> None:
    assert json.loads(_tools().get_template_request({"request_id": "x"})) == {"error": "invalid_request_id"}
    assert json.loads(_tools().list_template_requests({"estado": "otra"})) == {"error": "invalid_query"}
    assert json.loads(_tools().list_template_requests({"limit": 0})) == {"error": "invalid_query"}
    assert json.loads(_tools().validate_template({"request_id": REQUEST})) == {"error": "invalid_body"}
    assert bridge.seen == []


def test_reading_a_request_and_its_not_found_are_closed(bridge: type[_Bridge]) -> None:
    bridge.routes[f"GET {PREFIX}/template-requests/{REQUEST}"] = (200, _tpl({"request_id": REQUEST}))
    assert json.loads(_tools().get_template_request({"request_id": REQUEST}))["data"]["request_id"] == REQUEST
    bridge.routes[f"GET {PREFIX}/template-requests/{REQUEST}"] = (404, {"detail": "meta_template_request_not_found"})
    assert json.loads(_tools().get_template_request({"request_id": REQUEST})) == {
        "error": "meta_template_request_not_found"}


@pytest.mark.parametrize("code", ["meta_template_open_limit", "meta_template_name_taken",
                                  "meta_template_revision_mismatch", "meta_template_not_draft"])
def test_the_closed_codes_of_the_template_routes_come_back_as_they_are(bridge: type[_Bridge], code: str) -> None:
    bridge.routes[f"POST {PREFIX}/template-requests"] = (409, {"detail": code})
    assert json.loads(_tools().propose_template({"template": _template(), "purpose": "x"})) == {"error": code}


def test_the_template_tools_have_no_route_to_meta_nor_to_the_page() -> None:
    source = (PLUGIN / "tools.py").read_text(encoding="utf-8")
    assert "graph.facebook.com" not in source
    assert "/send" not in source and "/discard" not in source


# ------------------------------------------------------------- las campanas (E-355)


def test_a_bridge_without_the_campaigns_says_so_before_anything_is_created(bridge: type[_Bridge]) -> None:
    # Sin AGENT_CAMPAIGNS_ENABLED las rutas no existen: FastAPI contesta su 404 generico.
    bridge.routes[f"GET {PREFIX}/campaigns?estado=abiertas"] = (404, {"detail": "Not Found"})
    assert json.loads(_tools().list_campaigns({})) == {"error": "campaigns_not_enabled"}
    bridge.routes[f"POST {CHATWOOT}/macros"] = (405, {"detail": "Method Not Allowed"})
    assert json.loads(_tools().create_chatwoot_label_macro({"label": "promo"})) == {"error": "chatwoot_tool_not_enabled"}
    # La campana que no existe, con la ruta prendida, sigue siendo su codigo.
    bridge.routes[f"GET {PREFIX}/campaigns/{PROPOSAL}"] = (404, {"detail": "agent_campaign_not_found"})
    assert json.loads(_tools().get_campaign({"campaign_id": PROPOSAL})) == {"error": "agent_campaign_not_found"}


def test_the_campaign_tools_go_to_their_routes_with_who_asks_from_the_session(bridge: type[_Bridge]) -> None:
    tools = _tools()
    def respond(status: int, data: dict[str, object]) -> tuple[int, object]:
        return status, {"contract": "agent-campaigns-v1", "contract_version": "1.0.0", "data": data}

    bridge.routes[f"POST {PREFIX}/campaigns/preview"] = respond(200, {"ok": True})
    bridge.routes[f"POST {PREFIX}/campaigns"] = respond(201, {"campaign_id": PROPOSAL})
    bridge.routes[f"POST {PREFIX}/campaigns/{PROPOSAL}/validate"] = respond(200, {"status": "validada"})
    assert json.loads(tools.preview_campaign({"campaign": {"clave": "x"}}))["data"]["ok"] is True
    assert json.loads(tools.propose_campaign({"campaign": {"clave": "x"}, "purpose": "Black November"}))["data"]["campaign_id"] == PROPOSAL
    assert json.loads(str(bridge.seen[-1]["body"]))["requested_by"] == {"slack_user": "U0123456789"}
    assert json.loads(tools.validate_campaign({"campaign_id": PROPOSAL, "revision": 1}))["data"]["status"] == "validada"


def test_the_campaign_tools_never_send_a_phone_in_the_purpose(bridge: type[_Bridge]) -> None:
    tools = _tools()
    assert json.loads(tools.propose_campaign({"campaign": {}, "purpose": "Llamar al 5512345678"})) == {
        "error": "identifier_not_allowed"}


def test_the_campaign_names_its_own_offer_and_the_bridge_accepts_it() -> None:
    # E-410: oferta.codigo_oferta, la oferta de Hotmart de la campana (el off= del link de su pagina).
    from bridge.agent_campaigns import parse_campaign

    schemas = sys.modules[_load_plugin().__name__ + ".schemas"]
    description = schemas._CAMPAIGN["description"]
    assert "codigo_oferta" in description
    assert "declarada en la instancia" in description
    draft = {
        "clave": "black-november-2026", "nombre": "Black November", "etiqueta": "black-november",
        "macro_etiqueta": 77, "desde": "2026-11-20T00:00:00-06:00", "hasta": "2026-12-01T00:00:00-06:00",
        "palabras_clave": ["Black November"], "oferta": {"codigo_oferta": "wmkq4ba9"},
        "asesoramiento": ["Si pregunta por el bono, el de la página de Black."],
    }
    assert parse_campaign(draft)["oferta"] == {"codigo_oferta": "wmkq4ba9"}
