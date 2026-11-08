"""El boton «Revisar y aprobar» del Automation Expert (flow-change-cycle-v1 1.9.0, «El recorrido», paso 1).

El bridge falso contesta con el sobre de ``validate_change`` de ``src/bridge/flow_change_api.py``
(``proposal_id``, ``status``, ``fingerprint``, ``expires_at``, ``blocks``, ``warnings``,
``preview``). Slack se prueba con un servidor local que anota el ``chat.postMessage`` que recibe:
lo que se fija aca es el pedido que arma el plugin (es nuestro), y de la respuesta de Slack el
plugin mira solo ``ok``. El fixture capturado de esa respuesta, y del aviso de clic del boton de
URL, queda para el E2E con la app del Automation Expert (la regla 2 de la politica: no hay una
captura de Slack en ``tests/fixtures/`` y esta tarea no puede hablarle a Slack).
"""

from __future__ import annotations

import asyncio
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
BOT_TOKEN = "xoxb-" + "1" * 12 + "-" + "a" * 24
PROPOSAL = "5d0c2b9e-1f3a-4b6c-8d7e-9f0a1b2c3d4e"
PREFIX = "/internal/automation-expert/v1"
DM = "D0123456789"
THREAD = "1761600000.123456"
PURPOSE_MARK = "PARA-QUE-DEL-MODELO"


def _load_plugin() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "automation_expert_change_plugin_button", PLUGIN / "__init__.py",
        submodule_search_locations=[str(PLUGIN)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _modules() -> tuple[ModuleType, ModuleType, ModuleType]:
    plugin = _load_plugin()
    return plugin, sys.modules[plugin.__name__ + ".tools"], sys.modules[plugin.__name__ + ".approval_button"]


class _Server(BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, object]] = {}
    seen: list[dict[str, object]] = []

    def log_message(self, *args: object) -> None:  # noqa: D401 - silencio
        return

    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8") if length else ""
        type(self).seen.append({"method": self.command, "path": self.path, "body": body,
                                "authorization": self.headers.get("Authorization")})
        status, payload = type(self).routes.get(f"{self.command} {self.path}", (404, {"detail": "flow_change_not_found"}))
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


def _serve() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Server)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"127.0.0.1:{server.server_address[1]}"


@pytest.fixture()
def world(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    _Server.routes = {}
    _Server.seen = []
    server, host = _serve()
    plugin, tools, button = _modules()
    monkeypatch.setattr(button, "SLACK_POST_MESSAGE", f"http://{host}/api/chat.postMessage")
    for name, value in {
        "AUTOMATION_EXPERT_API_URL": f"http://{host}",
        "AUTOMATION_EXPERT_INTERNAL_HOST": host,
        "AUTOMATION_EXPERT_CHANGE_API_TOKEN": TOKEN,
        "HERMES_SESSION_PLATFORM": "slack",
        "HERMES_SESSION_USER_ID": "U0123456789",
        "HERMES_SESSION_CHAT_ID": DM,
        "HERMES_SESSION_THREAD_ID": THREAD,
        "SLACK_BOT_TOKEN": BOT_TOKEN,
        "FLOW_APPROVAL_ENABLED": "true",
        "FLOW_APPROVAL_BASE_URL": "https://bridge.example.org",
    }.items():
        monkeypatch.setenv(name, value)
    try:
        yield SimpleNamespace(plugin=plugin, tools=tools, button=button)
    finally:
        server.shutdown()


def _validated(status: str = "validada", blocks: list[object] | None = None) -> dict[str, object]:
    return {"contract": "flow-change-cycle-v1", "contract_version": "1.9.0", "data": {
        "proposal_id": PROPOSAL, "status": status, "fingerprint": "f" * 64,
        "expires_at": "2026-10-29T00:00:00Z", "blocks": blocks or [], "warnings": [],
        "preview": {"para_que": PURPOSE_MARK},
    }}


def _route(status: int, payload: object) -> None:
    _Server.routes[f"POST {PREFIX}/flow-changes/{PROPOSAL}/validate"] = (status, payload)


def _slack(ok: bool = True) -> None:
    _Server.routes["POST /api/chat.postMessage"] = (200, {"ok": ok, "channel": DM, "ts": "1761600001.000100"}
                                                    if ok else {"ok": False, "error": "channel_not_found"})


def _slack_calls() -> list[dict[str, object]]:
    return [seen for seen in _Server.seen if seen["path"] == "/api/chat.postMessage"]


def _validate(tools: ModuleType) -> dict[str, Any]:
    return json.loads(tools.validate_flow_change({"proposal_id": PROPOSAL, "revision": 3}))


def test_a_validated_proposal_posts_the_fixed_button_in_the_same_dm(world: SimpleNamespace) -> None:
    _route(200, _validated())
    _slack()
    out = _validate(world.tools)
    assert out["data"]["approval_button"] == world.button.POSTED
    [call] = _slack_calls()
    assert call["authorization"] == f"Bearer {BOT_TOKEN}"
    body = json.loads(str(call["body"]))
    url = f"https://bridge.example.org/flow-approval/p/{PROPOSAL}"
    assert body == {
        "channel": DM,
        "thread_ts": THREAD,
        "text": f"{world.button.FALLBACK_TEXT} {url}",
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": world.button.MESSAGE_TEXT}},
            {"type": "actions", "block_id": "flow_approval", "elements": [{
                "type": "button", "action_id": "flow_approval_open", "style": "primary",
                "text": {"type": "plain_text", "text": "Revisar y aprobar"}, "url": url,
            }]},
        ],
        "unfurl_links": False,
        "unfurl_media": False,
    }
    # Nada del modelo ni de la vista previa viaja a Slack, y el link no vuelve al agente.
    assert PURPOSE_MARK not in str(call["body"])
    assert "flow-approval" not in json.dumps(out["data"]["approval_button"])


def test_with_the_channel_off_nothing_is_posted_and_the_answer_is_the_same(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _route(200, _validated())
    _slack()
    for value in (None, "", "false", "1", "yes"):
        if value is None:
            monkeypatch.delenv("FLOW_APPROVAL_ENABLED")
        else:
            monkeypatch.setenv("FLOW_APPROVAL_ENABLED", value)
        assert _validate(world.tools)["data"] == _validated()["data"]
    assert _slack_calls() == []


def test_only_a_validated_proposal_without_blocks_gets_the_button(world: SimpleNamespace) -> None:
    _slack()
    for payload in (_validated(status="borrador"), _validated(blocks=[{"code": "template_missing"}]),
                    {**_validated(), "data": {**_validated()["data"], "proposal_id": "otro"}}):
        _route(200, payload)
        assert "approval_button" not in _validate(world.tools)["data"]
    _route(409, {"detail": "flow_change_revision_mismatch"})
    assert _validate(world.tools) == {"error": "flow_change_revision_mismatch"}
    _route(422, {"detail": "flow_change_draft_blocked", "data": {"blocks": [], "warnings": []}})
    assert _validate(world.tools)["error"] == "flow_change_draft_blocked"
    assert _slack_calls() == []


def test_slack_refusing_or_failing_reports_not_posted(world: SimpleNamespace) -> None:
    _route(200, _validated())
    _slack(ok=False)
    assert _validate(world.tools)["data"]["approval_button"] == world.button.NOT_POSTED
    # Slack caido o redirigiendo: tampoco, y la redireccion no se sigue.
    _Server.routes["POST /api/chat.postMessage"] = (302, {})
    assert _validate(world.tools)["data"]["approval_button"] == world.button.NOT_POSTED
    _Server.routes["POST /api/chat.postMessage"] = (500, {"ok": True})
    assert _validate(world.tools)["data"]["approval_button"] == world.button.NOT_POSTED
    assert not any(seen["path"] == "/robo" for seen in _Server.seen)


@pytest.mark.parametrize("name,value", [
    ("FLOW_APPROVAL_BASE_URL", "http://bridge.example.org"),
    ("FLOW_APPROVAL_BASE_URL", "https://bridge.example.org/otra/ruta"),
    ("FLOW_APPROVAL_BASE_URL", "https://bridge.example.org?x=1"),
    ("FLOW_APPROVAL_BASE_URL", "https://user:pass@bridge.example.org"),
    ("FLOW_APPROVAL_BASE_URL", ""),
    ("HERMES_SESSION_CHAT_ID", "C0123456789"),
    ("HERMES_SESSION_CHAT_ID", ""),
    ("SLACK_BOT_TOKEN", "xoxp-un-token-de-usuario"),
    ("SLACK_BOT_TOKEN", ""),
])
def test_without_a_safe_destination_nothing_is_posted(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, name: str, value: str,
) -> None:
    _route(200, _validated())
    _slack()
    monkeypatch.setenv(name, value)
    assert _validate(world.tools)["data"]["approval_button"] == world.button.NOT_POSTED
    assert _slack_calls() == []


def test_outside_slack_the_button_is_not_posted(world: SimpleNamespace) -> None:
    # Fuera de Slack la validacion ya corta antes (requested_by_unavailable); el boton tampoco sale solo.
    session = {"HERMES_SESSION_PLATFORM": "cli", "HERMES_SESSION_CHAT_ID": DM}
    settings = {"FLOW_APPROVAL_ENABLED": "true", "FLOW_APPROVAL_BASE_URL": "https://bridge.example.org",
                "SLACK_BOT_TOKEN": BOT_TOKEN}
    assert world.button.post_after_validation(
        PROPOSAL, setting=lambda name: settings.get(name, ""), session=lambda name: session.get(name, ""),
    ) == world.button.NOT_POSTED
    assert _slack_calls() == []


def test_without_a_thread_the_button_goes_to_the_dm(world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    _route(200, _validated())
    _slack()
    for thread in ("", "no-es-un-ts"):
        monkeypatch.setenv("HERMES_SESSION_THREAD_ID", thread)
        assert _validate(world.tools)["data"]["approval_button"] == world.button.POSTED
    assert all("thread_ts" not in json.loads(str(call["body"])) for call in _slack_calls())


def test_the_plugin_acks_the_url_button_click_and_adds_no_tool(world: SimpleNamespace) -> None:
    tools: list[str] = []
    actions: list[tuple[object, Any]] = []
    world.plugin.register(SimpleNamespace(
        profile_name="automation-expert",
        register_tool=lambda **kwargs: tools.append(kwargs["name"]),
        register_hook=lambda name, callback: None,
        register_slack_action_handler=lambda action_id, callback: actions.append((action_id, callback)),
    ))
    assert tools == ["get_flow_definitions", "propose_flow_change", "validate_flow_change", "get_flow_change",
                     "list_flow_changes", "list_templates", "propose_template", "validate_template",
                     "list_template_requests", "get_template_request", "list_chatwoot_labels", "create_chatwoot_label",
                     "add_chatwoot_label_to_conversation", "list_chatwoot_macros"]
    [(action_id, callback)] = actions
    assert action_id == "flow_approval_open"
    acked: list[bool] = []

    async def ack() -> None:
        acked.append(True)

    asyncio.run(callback(ack, {"actions": [{"action_id": action_id}]}, {"action_id": action_id}))
    assert acked == [True]
    # Un Hermes sin ese registro sigue cargando el plugin.
    world.plugin.register(SimpleNamespace(profile_name="automation-expert", register_tool=lambda **kwargs: None,
                                          register_hook=lambda name, callback: None))


# --- la pagina de una plantilla (template-request-v1, F-146 parte 5)


REQUEST = "7a1c2b9e-1f3a-4b6c-8d7e-9f0a1b2c3d4f"


def _validated_template(status: str = "validada", blocks: list[object] | None = None) -> dict[str, object]:
    return {"contract": "template-request-v1", "contract_version": "1.1.0",
            "data": {"request_id": REQUEST, "status": status, "fingerprint": "f" * 64,
                     "expires_at": "2026-11-09T12:00:00Z", "blocks": blocks or [], "warnings": []}}


def _template_route(payload: dict[str, object]) -> None:
    _Server.routes[f"POST {PREFIX}/template-requests/{REQUEST}/validate"] = (200, payload)


def _validate_template(tools: ModuleType) -> dict[str, Any]:
    return json.loads(tools.validate_template({"request_id": REQUEST, "revision": 1}))


def test_a_validated_template_posts_its_own_button_without_promising_a_notice(world: SimpleNamespace) -> None:
    _template_route(_validated_template())
    _slack()
    out = _validate_template(world.tools)
    assert out["data"]["approval_button"]["status"] == "posted"
    posted = json.loads(_slack_calls()[0]["body"])
    button = posted["blocks"][1]["elements"][0]
    assert button["url"] == f"https://bridge.example.org/flow-approval/t/{REQUEST}"
    assert button["text"]["text"] == "Revisar y mandar a Meta"
    text = posted["blocks"][0]["text"]["text"]
    assert "24 horas" in text and "avis" not in text.lower()


def test_a_template_with_blocks_gets_no_button(world: SimpleNamespace) -> None:
    _template_route(_validated_template(status="borrador", blocks=[{"code": "x"}]))
    _slack()
    out = _validate_template(world.tools)
    assert "approval_button" not in out["data"] and _slack_calls() == []


def test_slack_refusing_a_template_button_reports_not_posted(world: SimpleNamespace) -> None:
    _template_route(_validated_template())
    _slack(ok=False)
    assert _validate_template(world.tools)["data"]["approval_button"]["status"] == "not_posted"


def test_with_the_page_off_the_template_answer_has_no_button(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FLOW_APPROVAL_ENABLED", "false")
    _template_route(_validated_template())
    assert "approval_button" not in _validate_template(world.tools)["data"] and _slack_calls() == []


@pytest.mark.parametrize("name,value", [
    ("FLOW_APPROVAL_BASE_URL", "http://bridge.example.org"),
    ("FLOW_APPROVAL_BASE_URL", "https://bridge.example.org/otra/ruta"),
    ("FLOW_APPROVAL_BASE_URL", ""),
    ("HERMES_SESSION_CHAT_ID", "C0123456789"),
    ("HERMES_SESSION_CHAT_ID", ""),
    ("SLACK_BOT_TOKEN", "xoxp-un-token-de-usuario"),
    ("SLACK_BOT_TOKEN", ""),
])
def test_a_template_button_without_a_safe_destination_is_not_posted(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, name: str, value: str,
) -> None:
    _template_route(_validated_template())
    _slack()
    monkeypatch.setenv(name, value)
    assert _validate_template(world.tools)["data"]["approval_button"] == world.button.TEMPLATE_NOT_POSTED
    assert _slack_calls() == []


def test_a_template_button_outside_slack_is_not_posted(world: SimpleNamespace) -> None:
    # La herramienta ya corta antes (requested_by_unavailable): el boton tampoco sale solo.
    session = {"HERMES_SESSION_PLATFORM": "cli", "HERMES_SESSION_CHAT_ID": DM}
    settings = {"FLOW_APPROVAL_ENABLED": "true", "FLOW_APPROVAL_BASE_URL": "https://bridge.example.org",
                "SLACK_BOT_TOKEN": BOT_TOKEN}
    assert world.button.post_template_after_validation(
        REQUEST, setting=lambda name: settings.get(name, ""), session=lambda name: session.get(name, ""),
    ) == world.button.TEMPLATE_NOT_POSTED
    assert _slack_calls() == []


def test_a_template_button_goes_to_the_thread_only_with_a_real_ts(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _template_route(_validated_template())
    _slack()
    _validate_template(world.tools)
    assert json.loads(str(_slack_calls()[-1]["body"]))["thread_ts"] == THREAD
    for thread in ("", "no-es-un-ts"):
        monkeypatch.setenv("HERMES_SESSION_THREAD_ID", thread)
        assert _validate_template(world.tools)["data"]["approval_button"] == world.button.TEMPLATE_POSTED
        assert "thread_ts" not in json.loads(str(_slack_calls()[-1]["body"]))


def test_not_posted_does_not_send_the_agent_down_a_road_that_does_not_exist(world: SimpleNamespace) -> None:
    note = world.button.TEMPLATE_NOT_POSTED["note"]
    assert "operador" not in note and "no se reemite" in note
