"""El profile automation-expert y su plugin de lectura (automation-expert-read-v1).

Las claves del config salen de Hermes 0.21.0 (NousResearch/hermes-agent 29112be). El cliente del
plugin se prueba contra un bridge falso de verdad: un servidor HTTP local que contesta con la forma
del contrato y anota lo que recibe.
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
import yaml

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles" / "automation-expert"
PLUGIN = PROFILE / "plugins" / "automation-expert-read"
TOKEN = "e" * 40
LEAD = "0b8f2c1e-4a1d-4c55-9b7f-1e2d3c4b5a69"
ULID = "01JABCDEFGHJKMNPQRSTVWXYZ0"


def _load_plugin() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "automation_expert_read_plugin", PLUGIN / "__init__.py",
        submodule_search_locations=[str(PLUGIN)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _tools() -> ModuleType:
    return sys.modules[_load_plugin().__name__ + ".tools"]


# --------------------------------------------------------------- el paquete


def test_the_profile_talks_only_by_direct_message_with_only_its_toolset() -> None:
    config = yaml.safe_load((PROFILE / "config.yaml").read_text(encoding="utf-8"))
    assert config["platform_toolsets"] == {
        "slack": ["automation_expert_read", "automation_expert_change", "no_mcp"],
        "cli": ["automation_expert_read", "automation_expert_change", "no_mcp"],
    }
    slack = config["slack"]
    # Un centinela que no es un canal real: vacio querria decir «todos».
    assert slack["allowed_channels"] and all(channel.startswith("CNONE") for channel in slack["allowed_channels"])
    assert slack["unauthorized_dm_behavior"] == "ignore"
    assert "allow_admin_from" in slack and "group_allow_admin_from" in slack
    assert config["memory"]["memory_enabled"] is False
    assert config["memory"]["user_profile_enabled"] is False
    assert config["auxiliary"]["background_review"]["enabled"] is False
    assert config["sessions"]["auto_prune"] is True and config["sessions"]["retention_days"] <= 14
    for toolset in ("memory", "skills", "terminal", "web", "delegation", "cronjob", "kanban"):
        assert toolset in config["agent"]["disabled_toolsets"]
    assert config["plugins"]["enabled"] == ["automation-expert-read", "automation-expert-change"]
    text = (PROFILE / "config.yaml").read_text(encoding="utf-8")
    assert "ALLOW_ALL" not in text and "ignored_channels" not in text
    # Lo de cada instancia lo completa el instalador.
    assert config["model"] == {"default": None, "provider": None}


def test_the_distribution_copies_the_plugin_and_names_its_variables() -> None:
    distribution = yaml.safe_load((PROFILE / "distribution.yaml").read_text(encoding="utf-8"))
    assert distribution["name"] == "automation-expert"
    assert distribution["author"] == "ai-appointment-setter"
    assert set(distribution["distribution_owned"]) == {"SOUL.md", "config.yaml", "plugins"}
    required = {item["name"] for item in distribution["env_requires"] if item.get("required", True)}
    assert required == {"SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_ALLOWED_USERS",
                        "AUTOMATION_EXPERT_API_URL", "AUTOMATION_EXPERT_API_TOKEN",
                        "AUTOMATION_EXPERT_CHANGE_API_TOKEN"}
    plugin = yaml.safe_load((PLUGIN / "plugin.yaml").read_text(encoding="utf-8"))
    # Hermes busca el plugin por el nombre de su carpeta.
    assert plugin["name"] == PLUGIN.name
    assert plugin["provides_tools"] == ["list_flows", "lead_history", "draw_flow", "read_library"]
    change = yaml.safe_load((PROFILE / "plugins" / "automation-expert-change" / "plugin.yaml").read_text(encoding="utf-8"))
    assert change["name"] == "automation-expert-change"
    assert change["provides_tools"] == ["get_flow_definitions", "propose_flow_change", "validate_flow_change",
                                        "get_flow_change", "list_flow_changes", "list_templates"]


def test_the_soul_never_asks_for_a_phone_or_a_mail() -> None:
    soul = (PROFILE / "SOUL.md").read_text(encoding="utf-8")
    assert "Nunca pidas un teléfono ni un mail" in soul
    assert "No aprobás, no publicás y no pausás" in soul


def test_the_registered_tools_and_hooks() -> None:
    plugin = _load_plugin()
    tools: list[str] = []
    hooks: dict[str, Any] = {}
    ctx = SimpleNamespace(
        profile_name="automation-expert",
        register_tool=lambda **kwargs: tools.append(kwargs["name"]) or None,
        register_hook=lambda name, callback: hooks.__setitem__(name, callback),
    )
    plugin.register(ctx)
    assert tools == ["list_flows", "lead_history", "draw_flow", "read_library"]
    dispatch = hooks["pre_gateway_dispatch"]
    slack = SimpleNamespace(value="slack")
    assert dispatch(event=SimpleNamespace(source=SimpleNamespace(platform=slack, chat_id="D0123"))) is None
    # Un canal o un mensaje directo de grupo no pasa, aunque llegue por un slash command.
    for chat_id in ("C0123", "G0123", ""):
        assert dispatch(event=SimpleNamespace(source=SimpleNamespace(platform=slack, chat_id=chat_id))) == {
            "action": "skip", "reason": "automation_expert_dm_only"}
    cli = SimpleNamespace(value="cli")
    assert dispatch(event=SimpleNamespace(source=SimpleNamespace(platform=cli, chat_id="x"))) is None
    assert hooks["pre_tool_call"](tool_name="lead_history") is None
    # En otro profile, las herramientas no corren.
    other: dict[str, Any] = {}
    plugin.register(SimpleNamespace(profile_name="agente-comercial", register_tool=lambda **kwargs: None,
                                    register_hook=lambda name, callback: other.__setitem__(name, callback)))
    blocked = other["pre_tool_call"](tool_name="lead_history")
    assert blocked["action"] == "block" and blocked["message"]
    assert other["pre_tool_call"](tool_name="otra_herramienta") is None


# --------------------------------------------------------------- el cliente


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
        status, payload = type(self).routes.get(f"{self.command} {self.path.split('?')[0]}", (404, {"detail": "lead_not_found"}))
        if status == 302:
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:9/robo")
            self.end_headers()
            return
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
    monkeypatch.setenv("AUTOMATION_EXPERT_API_TOKEN", TOKEN)
    try:
        yield _Bridge
    finally:
        server.shutdown()


def _envelope(data: object, version: str = "1.1.0") -> dict[str, object]:
    return {"contract": "automation-expert-read-v1", "contract_version": version, "data": data,
            "gaps": [{"code": "payment_slip_not_attached", "scope": "instance",
                      "what": "si imprimió una ficha", "why": "llamó desde 55 1234 5678 o ana@x.test"}]}


def test_the_flows_go_through_with_the_bearer(bridge: type[_Bridge]) -> None:
    bridge.routes["GET /internal/automation-expert/v1/flows"] = (200, _envelope({"flows": []}))
    result = json.loads(_tools().list_flows({}))
    assert result["data"] == {"flows": []}
    assert bridge.seen[0]["authorization"] == f"Bearer {TOKEN}"
    # El texto libre se tapa; el resto no se toca.
    assert result["gaps"][0]["why"] == "llamó desde <tapado> o <tapado>"


def test_the_lookup_sends_only_the_issuance_of_a_link(bridge: type[_Bridge]) -> None:
    bridge.routes["POST /internal/automation-expert/v1/leads/lookup"] = (
        200, _envelope({"result": "found", "lead_ref": LEAD, "candidates": []}))
    bridge.routes[f"GET /internal/automation-expert/v1/leads/{LEAD}/history"] = (
        200, _envelope({"lead": {"lead_ref": LEAD, "label": "Ana · conversación #501"}, "past": []}))
    link = (f"https://pay.hotmart.com/X?off=2uafw5bg&checkoutMode=10&src=hermes"
            f"&sck=algo~hermes~v1~{ULID}&fbclid=PRIVADO")
    result = json.loads(_tools().lead_history({"link": link}))
    assert result["data"]["lead"]["lead_ref"] == LEAD
    lookup, history = bridge.seen
    assert json.loads(lookup["body"]) == {"issuance": ULID}
    assert "PRIVADO" not in json.dumps(bridge.seen)
    assert history["path"] == f"/internal/automation-expert/v1/leads/{LEAD}/history?limit=50"


def test_several_candidates_come_back_without_a_second_request(bridge: type[_Bridge]) -> None:
    candidates = {"result": "ambiguous", "lead_ref": None,
                  "candidates": [{"lead_ref": LEAD, "label": "Ana · conversación #501"}]}
    bridge.routes["POST /internal/automation-expert/v1/leads/lookup"] = (200, _envelope(candidates))
    result = json.loads(_tools().lead_history({"name": "Ana"}))
    assert result["data"]["result"] == "ambiguous"
    assert len(bridge.seen) == 1
    # El lead_ref de un uuid nunca se tapa, aunque tenga siete digitos seguidos.
    assert result["data"]["candidates"][0]["lead_ref"] == LEAD


@pytest.mark.parametrize("params,error", [
    ({"name": "+52 55 1234 5678"}, "identifier_not_allowed"),
    ({"name": "ana@example.test"}, "identifier_not_allowed"),
    ({"conversation": 5512345678}, "identifier_not_allowed"),
    ({"conversation": "55 1234 5678"}, "identifier_not_allowed"),
    ({"conversation": 501, "name": "Ana"}, "invalid_lookup"),
    ({}, "invalid_lookup"),
    ({"link": "https://pay.hotmart.com/X?sck=otra-cosa"}, "invalid_lookup"),
    ({"lead_ref": "no-es-un-uuid"}, "invalid_lead_ref"),
    ({"lead_ref": LEAD, "limit": 0}, "limit_out_of_range"),
    ({"lead_ref": LEAD, "cursor": "!!"}, "invalid_cursor"),
])
def test_bad_keys_never_reach_the_network(bridge: type[_Bridge], params: dict[str, object], error: str) -> None:
    assert json.loads(_tools().lead_history(params)) == {"error": error}
    assert bridge.seen == []


def test_errors_map_to_closed_codes(bridge: type[_Bridge]) -> None:
    tools = _tools()
    bridge.routes["GET /internal/automation-expert/v1/flows/carrito"] = (401, {"detail": "automation_expert_authentication_required"})
    assert json.loads(tools.list_flows({"flow_key": "carrito"})) == {"error": "automation_expert_authentication_required"}
    bridge.routes["GET /internal/automation-expert/v1/flows/carrito"] = (503, {"detail": "automation_expert_busy"})
    assert json.loads(tools.list_flows({"flow_key": "carrito"})) == {"error": "automation_expert_busy"}
    # Un detalle que no es un codigo cerrado no pasa tal cual.
    bridge.routes["GET /internal/automation-expert/v1/flows/carrito"] = (500, {"detail": "Traceback 5512345678"})
    assert json.loads(tools.list_flows({"flow_key": "carrito"})) == {"error": "automation_expert_read_unavailable"}
    bridge.routes["GET /internal/automation-expert/v1/flows/carrito"] = (200, _envelope({}, version="2.0.0"))
    assert json.loads(tools.list_flows({"flow_key": "carrito"})) == {"error": "automation_expert_contract_unsupported"}
    assert json.loads(tools.list_flows({"flow_key": "Bad-Key"})) == {"error": "invalid_flow_key"}
    bridge.routes["GET /internal/automation-expert/v1/flows"] = (302, None)
    assert json.loads(tools.list_flows({})) == {"error": "automation_expert_read_unavailable"}
    assert all(seen["path"] != "/robo" for seen in bridge.seen)


@pytest.mark.parametrize("url,internal", [
    ("http://att1-bridge:8000", "att1-bridge:8001"),
    ("http://att1-bridge", "att1-bridge"),
    ("http://otro:8000", "att1-bridge:8000"),
    ("http://att1-bridge:8000", ""),
    ("https://user:pass@bridge.example.test", ""),
    ("https://bridge.example.test/otra/ruta", ""),
    ("https://bridge.example.test?x=1", ""),
    ("ftp://bridge.example.test", ""),
])
def test_http_only_to_the_exact_bridge_with_its_port(
    monkeypatch: pytest.MonkeyPatch, url: str, internal: str,
) -> None:
    monkeypatch.setenv("AUTOMATION_EXPERT_API_URL", url)
    monkeypatch.setenv("AUTOMATION_EXPERT_INTERNAL_HOST", internal)
    monkeypatch.setenv("AUTOMATION_EXPERT_API_TOKEN", TOKEN)
    assert json.loads(_tools().list_flows({})) == {"error": "automation_expert_api_url_invalid"}


def test_the_settings_come_from_the_profile_secret_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AUTOMATION_EXPERT_API_URL", "AUTOMATION_EXPERT_API_TOKEN", "AUTOMATION_EXPERT_INTERNAL_HOST"):
        monkeypatch.delenv(name, raising=False)
    scoped = {"AUTOMATION_EXPERT_API_URL": "http://att1-bridge:8000",
              "AUTOMATION_EXPERT_INTERNAL_HOST": "att1-bridge:8000",
              "AUTOMATION_EXPERT_API_TOKEN": TOKEN}
    secret_scope = ModuleType("agent.secret_scope")
    setattr(secret_scope, "get_secret", lambda name, default="": scoped.get(name, default))
    monkeypatch.setitem(sys.modules, "agent", ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.secret_scope", secret_scope)
    assert _tools()._api_settings() == ("http://att1-bridge:8000", TOKEN)
    scoped["AUTOMATION_EXPERT_API_TOKEN"] = "corto"
    with pytest.raises(ValueError, match="automation_expert_api_token_invalid"):
        _tools()._api_settings()
