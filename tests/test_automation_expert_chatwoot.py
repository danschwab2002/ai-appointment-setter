"""La herramienta acotada de Chatwoot del Automation Expert (src/bridge/automation_expert_chatwoot.py, F-143).

Chatwoot no se reemplaza con un falso que conteste lo que la prueba quiere: la
app usa el cliente real (``ScopedChatwoot``) y Chatwoot se emula con
``httpx.MockTransport``, con las reglas de v4.13.0 que importan aca (el titulo
unico por cuenta, la lista de etiquetas de una conversacion que se REEMPLAZA
con el POST, la conversacion buscada por su numero).

Fixtures:

* las conversaciones, capturadas: la 50 de la bandeja 11 de ATT1 (cuenta 2) el
  2026-10-17 y la 158 de la bandeja 9 de Johanna el 2026-09-24;
* los macros 4, 5 y 6 de la cuenta 2, capturados el 2026-10-16, en la forma
  del index;
* las etiquetas y el POST de una etiqueta: **fixture de documentacion, falta
  captura real** (tests/fixtures/automation_expert_chatwoot/).
"""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_flow_change_api_app import (
    FakeChatwoot,
    PostgrestEmulator,
    SharedSupabaseSpy,
)
from test_flow_change_api_app import _settings as _change_settings

from bridge import automation_expert_chatwoot as tool
from bridge import flow_change_api
from bridge.app import Settings, create_app

FIXTURES = Path(__file__).parent / "fixtures"
DOC = json.loads((FIXTURES / "automation_expert_chatwoot" /
                  "chatwoot_v4_13_labels_macros_documentacion_20261101.json").read_text())
MACRO_DOC = json.loads((FIXTURES / "automation_expert_chatwoot" /
                        "chatwoot_v4_13_macro_create_documentacion_20261108.json").read_text())
CONV_50 = json.loads((FIXTURES / "chatwoot_conversation_inbox_11_conv_50_opt_out_paused_20261017.json")
                     .read_text())["conversation"]["conversation"]
CONV_158 = json.loads((FIXTURES / "chatwoot_conversation_api_show_conv_158_20260924.json").read_text())["conversation"]
TOKEN = "c" * 40
READ_TOKEN = "e" * 40
CONTROL = "k" * 24
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SLACK = {"slack_user": "U0123456789"}
PREFIX = "/internal/automation-expert/v1/chatwoot"
ACCOUNT = 2
INBOX = 11


class ChatwootEmulator:
    """La cuenta 2 de Chatwoot 4.13 con lo que la herramienta toca."""

    def __init__(self) -> None:
        self.labels = copy.deepcopy(DOC["labels_index_account_2"]["body"]["payload"])
        self.conversations = {50: copy.deepcopy(CONV_50), 158: copy.deepcopy(CONV_158)}
        self.conversation_labels = {50: list(CONV_50["labels"]), 158: list(CONV_158["labels"])}
        self.requests: list[tuple[str, str, object]] = []
        self.create_race = False  # otro crea la etiqueta entre la lectura y la escritura
        self.created_macros: list[dict] = []
        self.macro_reject: int | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body))
        assert request.headers["api_access_token"] == CONTROL
        path = request.url.path.removeprefix(f"/api/v1/accounts/{ACCOUNT}")
        if request.method == "GET" and path == "/labels":
            return httpx.Response(200, json={"payload": copy.deepcopy(self.labels)})
        if request.method == "POST" and path == "/labels":
            title = body["title"].lower()
            if self.create_race:
                self.labels.append({"id": 77, "title": title, "description": None, "color": "#1f93ff",
                                    "show_on_sidebar": True})
            if any(label["title"] == title for label in self.labels):
                return httpx.Response(422, json=DOC["label_create_duplicate"]["body"])
            label = {"id": 10, "title": title, "description": body.get("description"),
                     "color": body.get("color", "#1f93ff"), "show_on_sidebar": body.get("show_on_sidebar")}
            self.labels.append(label)
            return httpx.Response(200, json=label)
        if request.method == "GET" and path == "/macros":
            macros = copy.deepcopy(DOC["macros_index_account_2"]["body"])
            macros["payload"] += copy.deepcopy(self.created_macros)
            return httpx.Response(200, json=macros)
        if request.method == "POST" and path == "/macros":
            assert set(body) == {"name", "visibility", "actions"} and body["visibility"] == "global"
            if self.macro_reject is not None:
                return httpx.Response(self.macro_reject, json={"error": "no"})
            macro = copy.deepcopy(MACRO_DOC["response_200"]["payload"])
            macro.update(id=9000 + len(self.created_macros) + 1, name=body["name"], actions=body["actions"])
            self.created_macros.append(macro)
            return httpx.Response(200, json={"payload": macro})
        parts = path.split("/")
        if len(parts) >= 3 and parts[1] == "conversations":
            number = int(parts[2])
            if number not in self.conversations:
                return httpx.Response(404, json={"error": "Resource could not be found"})
            if request.method == "GET" and len(parts) == 3:
                return httpx.Response(200, json=copy.deepcopy(self.conversations[number]))
            if len(parts) == 4 and parts[3] == "labels":
                if request.method == "POST":
                    # update_labels: la lista que llega reemplaza la que habia.
                    self.conversation_labels[number] = list(body["labels"])
                return httpx.Response(200, json={"payload": list(self.conversation_labels[number])})
        return httpx.Response(599, json={"error": "la prueba no espera este pedido"})


def _scoped(emulator: ChatwootEmulator, *, create_macros: bool = False) -> tool.ScopedChatwoot:
    return tool.ScopedChatwoot(base_url="https://chatwoot.example.org", account_id=ACCOUNT, access_token=CONTROL,
                               inbox_id=INBOX, transport=httpx.MockTransport(emulator.handler), create_macros=create_macros)


def _settings(**overrides: object) -> Settings:
    base = _change_settings(chatwoot_account_id=ACCOUNT, chatwoot_control_api_access_token=CONTROL,
                            automation_expert_chatwoot_enabled=True)
    return replace(base, **overrides)


def _client(emulator: ChatwootEmulator | None = None, **overrides: object) -> tuple[TestClient, ChatwootEmulator]:
    emulator = emulator or ChatwootEmulator()
    store = flow_change_api.FlowChangeStore(base_url="https://att1-rest.internal", db_key="x",
                                            transport=httpx.MockTransport(PostgrestEmulator().handler))
    app = create_app(_settings(**overrides), supabase_client=SharedSupabaseSpy(),  # type: ignore[arg-type]
                     chatwoot_client=FakeChatwoot(), flow_change_store=store,  # type: ignore[arg-type]
                     automation_expert_chatwoot_client=_scoped(emulator))
    return TestClient(app), emulator


# ----------------------------------------------------------------- la lista cerrada


def test_the_closed_list_is_exactly_the_decided_one() -> None:
    assert tool.ALLOWED_CALLS == (
        ("GET", "/api/v1/accounts/{account}/labels"),
        ("POST", "/api/v1/accounts/{account}/labels"),
        ("GET", "/api/v1/accounts/{account}/conversations/{conversation}"),
        ("GET", "/api/v1/accounts/{account}/conversations/{conversation}/labels"),
        ("POST", "/api/v1/accounts/{account}/conversations/{conversation}/labels"),
        ("GET", "/api/v1/accounts/{account}/macros"),
        ("POST", "/api/v1/accounts/{account}/macros"),
    )
    assert not any(method == "DELETE" for method, _ in tool.ALLOWED_CALLS)


@pytest.mark.parametrize("path", [
    "/api/v1/accounts/2/labels", "/api/v1/accounts/2/labels/10", "/api/v1/accounts/2/conversations/50",
    "/api/v1/accounts/2/conversations/50/labels", "/api/v1/accounts/2/macros", "/api/v1/accounts/2/macros/12",
])
def test_every_delete_is_refused_before_the_network(path: str) -> None:
    emulator = ChatwootEmulator()
    scoped = _scoped(emulator)
    for method in ("DELETE", "delete", "Delete"):
        with pytest.raises(tool.ChatwootCallRefused) as refused:
            scoped.check_call(method, path)
        assert refused.value.code == "chatwoot_delete_not_allowed"
    with pytest.raises(tool.ChatwootCallRefused):
        asyncio.run(scoped._call("DELETE", path))
    assert emulator.requests == []


@pytest.mark.parametrize(("method", "path"), [
    ("POST", "/api/v1/accounts/2/macros/12/execute"),            # ejecutar un macro
    ("PATCH", "/api/v1/accounts/2/labels/10"),                   # cambiar una etiqueta
    ("PUT", "/api/v1/accounts/2/labels/10"),
    ("GET", "/api/v1/accounts/2/labels/10"),
    ("POST", "/api/v1/accounts/2/conversations/50/messages"),     # mandar un mensaje
    ("POST", "/api/v1/accounts/2/conversations/50/assignments"),  # reasignar
    ("POST", "/api/v1/accounts/2/conversations/50/toggle_status"),
    ("GET", "/api/v1/accounts/2/contacts/search"),
    ("GET", "/api/v1/accounts/3/labels"),                        # otra cuenta que la clave tambien administra
    ("POST", "/api/v1/accounts/1/conversations/50/labels"),
    ("GET", "/api/v1/accounts/2/conversations/0/labels"),
    ("GET", "/api/v1/accounts/2/conversations/50/labels?x=1"),
    ("GET", "/api/v1/accounts/2/conversations/../labels"),
    ("GET", "/api/v1/accounts/2/labels/"),
])
def test_everything_outside_the_list_is_refused(method: str, path: str) -> None:
    with pytest.raises(tool.ChatwootCallRefused) as refused:
        _scoped(ChatwootEmulator()).check_call(method, path)
    assert refused.value.code == "chatwoot_call_not_allowed"


def test_the_list_lets_through_its_own_calls_on_its_own_account() -> None:
    scoped = _scoped(ChatwootEmulator(), create_macros=True)
    for method, template in tool.ALLOWED_CALLS:
        scoped.check_call(method, template.format(account=ACCOUNT, conversation=50))


# ----------------------------------------------------------------- el flag


def test_with_the_flag_off_the_routes_do_not_exist_and_ready_does_not_name_them() -> None:
    client, emulator = _client(automation_expert_chatwoot_enabled=False)
    assert client.get(f"{PREFIX}/labels", headers=AUTH).status_code == 404
    assert client.post(f"{PREFIX}/labels", headers=AUTH, json={"title": "cliente", "requested_by": SLACK}) \
        .status_code == 404
    assert "automation_expert_chatwoot" not in json.dumps(client.get("/ready").json())
    assert emulator.requests == []


def test_ready_names_it_and_the_read_token_does_not_open_it() -> None:
    client, emulator = _client()
    assert '"automation_expert_chatwoot": "enabled"' in json.dumps(client.get("/ready").json())
    assert client.get(f"{PREFIX}/labels").status_code == 401
    assert client.get(f"{PREFIX}/labels", headers={"Authorization": f"Bearer {READ_TOKEN}"}).status_code == 401
    assert emulator.requests == []


@pytest.mark.parametrize(("overrides", "message"), [
    ({"automation_expert_change_enabled": False, "automation_expert_change_token": None,
      "automation_expert_change_db_key": None}, "requires AUTOMATION_EXPERT_CHANGE_ENABLED"),
    ({"chatwoot_control_api_access_token": None}, "requires the Chatwoot control access token"),
    ({"chatwoot_account_id": 3}, "CHATWOOT_ACCOUNT_ID to match the manifest"),
])
def test_the_bridge_does_not_start_with_a_wrong_configuration(overrides: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _client(**overrides)


def test_the_flag_is_a_portable_capability() -> None:
    from bridge import app as app_module

    assert "automation_expert_chatwoot_enabled" in app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES


# ----------------------------------------------------------------- las etiquetas


def test_listing_labels_reads_the_account_of_the_manifest() -> None:
    client, emulator = _client()
    response = client.get(f"{PREFIX}/labels", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["contract"] == "automation-expert-chatwoot-v1"
    assert body["instance"] == {"tenant_ref": "lancemos", "ally_ref": "att1"}
    assert [label["title"] for label in body["data"]["labels"]] == [
        "automation_opted_out", "automation_paused", "producto_att1"]
    assert emulator.requests == [("GET", "/api/v1/accounts/2/labels", None)]


def test_creating_a_label_posts_only_the_four_fields_and_says_it_was_created() -> None:
    client, emulator = _client()
    response = client.post(f"{PREFIX}/labels", headers=AUTH,
                           json={"title": " Cliente ", "description": "Compro el producto", "requested_by": SLACK})
    assert response.status_code == 201
    assert response.json()["data"] == {"created": True, "label": {
        "id": 10, "title": "cliente", "description": "Compro el producto", "color": "#1f93ff",
        "show_on_sidebar": True}}
    assert emulator.requests[-1] == ("POST", "/api/v1/accounts/2/labels", {
        "title": "cliente", "description": "Compro el producto", "color": "#1f93ff", "show_on_sidebar": True})


def test_a_label_that_already_exists_is_not_touched() -> None:
    client, emulator = _client()
    response = client.post(f"{PREFIX}/labels", headers=AUTH, json={"title": "producto_att1", "requested_by": SLACK})
    assert response.status_code == 200 and response.json()["data"]["created"] is False
    assert [method for method, _, _ in emulator.requests] == ["GET"]


def test_a_label_created_by_someone_else_meanwhile_is_not_an_error() -> None:
    emulator = ChatwootEmulator()
    emulator.create_race = True
    client, _ = _client(emulator)
    response = client.post(f"{PREFIX}/labels", headers=AUTH, json={"title": "cliente", "requested_by": SLACK})
    assert response.status_code == 200 and response.json()["data"]["created"] is False
    assert [method for method, _, _ in emulator.requests] == ["GET", "POST", "GET"]


@pytest.mark.parametrize(("body", "code"), [
    ({"title": "automation_paused"}, "chatwoot_label_reserved"),
    ({"title": "AUTOMATION_opted_out"}, "chatwoot_label_reserved"),
    ({"title": "con espacio"}, "invalid_body"),
    ({"title": "x" * 41}, "invalid_body"),
    ({"title": "5215512345678"}, "identifier_not_allowed"),
    ({"title": "cliente", "description": "llamar al 55 1234 5678"}, "identifier_not_allowed"),
    ({"title": "cliente", "color": "rojo"}, "invalid_body"),
    ({"title": "cliente", "show_on_sidebar": "si"}, "invalid_body"),
    ({"title": "cliente", "account_id": 3}, "invalid_body"),
])
def test_a_label_that_does_not_go_is_refused_before_chatwoot(body: dict, code: str) -> None:
    client, emulator = _client()
    response = client.post(f"{PREFIX}/labels", headers=AUTH, json={**body, "requested_by": SLACK})
    assert response.status_code == 422 and response.json()["detail"] == code
    assert emulator.requests == []


def test_without_who_asks_nothing_is_created() -> None:
    client, emulator = _client()
    response = client.post(f"{PREFIX}/labels", headers=AUTH, json={"title": "cliente"})
    assert response.status_code == 422 and response.json()["reason"]["missing"] == ["requested_by"]
    assert emulator.requests == []


# ----------------------------------------------------------------- la etiqueta en una conversacion


def test_adding_a_label_keeps_every_label_the_conversation_had() -> None:
    emulator = ChatwootEmulator()
    emulator.labels.append({"id": 10, "title": "cliente", "description": None, "color": "#1f93ff",
                            "show_on_sidebar": True})
    client, _ = _client(emulator)
    response = client.post(f"{PREFIX}/conversations/50/labels", headers=AUTH,
                           json={"label": "cliente", "requested_by": SLACK})
    assert response.status_code == 200
    assert response.json()["data"] == {"conversation_id": 50, "label": "cliente", "changed": True,
                                       "labels": ["automation_paused", "cliente"]}
    # Chatwoot reemplaza la lista: la pausa que tenia la conversacion 50 viaja en el POST.
    assert emulator.requests[-1] == ("POST", "/api/v1/accounts/2/conversations/50/labels",
                                     {"labels": ["automation_paused", "cliente"]})
    assert emulator.conversation_labels[50] == ["automation_paused", "cliente"]


def test_a_label_the_conversation_already_has_is_not_posted() -> None:
    emulator = ChatwootEmulator()
    emulator.labels.append({"id": 10, "title": "cliente", "description": None, "color": "#1f93ff",
                            "show_on_sidebar": True})
    emulator.conversation_labels[50] = ["automation_paused", "cliente"]
    client, _ = _client(emulator)
    response = client.post(f"{PREFIX}/conversations/50/labels", headers=AUTH,
                           json={"label": "cliente", "requested_by": SLACK})
    assert response.json()["data"]["changed"] is False
    assert all(method == "GET" for method, _, _ in emulator.requests)


def test_a_conversation_of_another_inbox_is_as_if_it_did_not_exist() -> None:
    emulator = ChatwootEmulator()
    client, _ = _client(emulator)
    # La 158 es de la bandeja 9 (Johanna): con la cuenta fija y la bandeja 11, no se toca.
    response = client.post(f"{PREFIX}/conversations/158/labels", headers=AUTH,
                           json={"label": "producto_att1", "requested_by": SLACK})
    assert response.status_code == 404 and response.json()["detail"] == "chatwoot_conversation_not_found"
    missing = client.post(f"{PREFIX}/conversations/999/labels", headers=AUTH,
                          json={"label": "producto_att1", "requested_by": SLACK})
    assert missing.status_code == 404 and missing.json()["detail"] == "chatwoot_conversation_not_found"
    assert not any(method == "POST" for method, _, _ in emulator.requests)


@pytest.mark.parametrize(("conversation", "label", "status", "code"), [
    ("50", "no_existe", 404, "chatwoot_label_not_found"),
    ("50", "automation_paused", 422, "chatwoot_label_reserved"),
    ("50", "automation_opted_out", 422, "chatwoot_label_reserved"),
    ("0", "cliente", 422, "invalid_conversation_id"),
    ("050", "cliente", 422, "invalid_conversation_id"),
    ("abc", "cliente", 422, "invalid_conversation_id"),
])
def test_what_does_not_go_never_reaches_a_post(conversation: str, label: str, status: int, code: str) -> None:
    client, emulator = _client()
    response = client.post(f"{PREFIX}/conversations/{conversation}/labels", headers=AUTH,
                           json={"label": label, "requested_by": SLACK})
    assert response.status_code == status and response.json()["detail"] == code
    assert not any(method == "POST" for method, _, _ in emulator.requests)


# ----------------------------------------------------------------- los macros


def test_macros_come_without_who_made_them_and_with_personal_params_hidden() -> None:
    client, emulator = _client()
    response = client.get(f"{PREFIX}/macros", headers=AUTH)
    assert response.status_code == 200
    macros = response.json()["data"]["macros"]
    assert macros[0] == {"id": 4, "name": "Pausar", "visibility": "global",
                         "actions": [{"action_name": "add_label", "action_params": ["automation_paused"]}]}
    assert macros[3]["actions"] == [{"action_name": "send_email_transcript", "action_params": ["<oculto>"]},
                                    {"action_name": "assign_team", "action_params": [3]}]
    text = json.dumps(response.json())
    assert "example.org" not in text and "created_by" not in text and "Persona" not in text
    assert emulator.requests == [("GET", "/api/v1/accounts/2/macros", None)]


def test_a_chatwoot_that_does_not_answer_is_503_with_a_closed_code() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("caido", request=request)

    scoped = tool.ScopedChatwoot(base_url="https://chatwoot.example.org", account_id=ACCOUNT, access_token=CONTROL,
                                 inbox_id=INBOX, transport=httpx.MockTransport(down))
    store = flow_change_api.FlowChangeStore(base_url="https://att1-rest.internal", db_key="x",
                                            transport=httpx.MockTransport(PostgrestEmulator().handler))
    app = create_app(_settings(), supabase_client=SharedSupabaseSpy(),  # type: ignore[arg-type]
                     chatwoot_client=FakeChatwoot(), flow_change_store=store,  # type: ignore[arg-type]
                     automation_expert_chatwoot_client=scoped)
    client = TestClient(app)
    for path in ("/labels", "/macros"):
        response = client.get(f"{PREFIX}{path}", headers=AUTH)
        assert response.status_code == 503 and response.json()["detail"] == "chatwoot_tool_unavailable"


# ---------------------------------------------------- crear el macro de una campana (E-355)


def _macro(client: TestClient, label: str = "promo") -> httpx.Response:
    return client.post(f"{PREFIX}/macros", json={"label": label, "requested_by": SLACK}, headers=AUTH)


def _macro_client(emulator: ChatwootEmulator | None = None) -> tuple[TestClient, ChatwootEmulator]:
    # Las rutas de una instancia con campanas. create_app las arma asi con AGENT_CAMPAIGNS_ENABLED
    # (test_agent_campaign_routes prueba ese cableado).
    emulator = emulator or ChatwootEmulator()
    app = FastAPI()
    tool.register_automation_expert_chatwoot_routes(
        app, token=TOKEN, chatwoot=_scoped(emulator, create_macros=True), tenant_ref="lancemos", ally_ref="att1",
        create_macros=True)
    return TestClient(app), emulator


def test_without_campaigns_there_is_no_macro_creation_and_the_contract_stays_the_same() -> None:
    client, emulator = _client()
    _with_label(emulator)
    response = _macro(client)
    # GET /macros sigue existiendo: el POST da 405 y el plugin lo dice como herramienta no prendida.
    assert response.status_code == 405 and response.json()["detail"] == "Method Not Allowed"
    assert not any(method == "POST" for method, _, _ in emulator.requests)
    assert client.get(f"{PREFIX}/labels", headers=AUTH).json()["contract_version"] == "1.0.0"
    assert client.get(f"{PREFIX}/macros", headers=AUTH).status_code == 200


def test_the_closed_list_does_not_let_a_macro_be_created_without_campaigns() -> None:
    with pytest.raises(tool.ChatwootCallRefused) as refused:
        _scoped(ChatwootEmulator()).check_call("POST", "/api/v1/accounts/2/macros")
    assert refused.value.code == "chatwoot_call_not_allowed"
    tool.ScopedChatwoot(base_url="https://chatwoot.example.org", account_id=ACCOUNT, access_token=CONTROL, inbox_id=INBOX,
                        create_macros=True).check_call("POST", "/api/v1/accounts/2/macros")


def test_a_chatwoot_answer_with_other_actions_is_not_taken_as_the_macro() -> None:
    class Tampered(ChatwootEmulator):
        def handler(self, request: httpx.Request) -> httpx.Response:
            response = super().handler(request)
            if request.method == "POST" and request.url.path.endswith("/macros"):
                body = response.json()
                body["payload"]["actions"].append({"action_name": "resolve_conversation", "action_params": []})
                return httpx.Response(200, json=body)
            return response

    client, emulator = _macro_client(Tampered())
    _with_label(emulator)
    response = _macro(client)
    assert response.status_code == 503 and response.json()["detail"] == "chatwoot_tool_response_invalid"


def test_with_campaigns_the_contract_says_it_can_create_macros() -> None:
    client, _ = _macro_client()
    assert client.get(f"{PREFIX}/labels", headers=AUTH).json()["contract_version"] == "1.1.0"


def _with_label(emulator: ChatwootEmulator, title: str = "promo") -> None:
    emulator.labels.append({"id": 88, "title": title, "description": None, "color": "#1f93ff", "show_on_sidebar": True})


def test_it_creates_a_macro_that_only_adds_the_label_and_with_the_body_chatwoot_expects() -> None:
    client, emulator = _macro_client()
    _with_label(emulator)
    response = _macro(client)
    assert response.status_code == 201, response.text
    assert response.json()["data"]["created"] is True
    assert response.json()["data"]["macro"]["actions"] == [{"action_name": "add_label", "action_params": ["promo"]}]
    assert ("POST", "/api/v1/accounts/2/macros", {"name": "campana-promo", "visibility": "global", "actions": [
        {"action_name": "add_label", "action_params": ["promo"]}]}) in emulator.requests


def test_a_second_ask_returns_the_same_macro_without_creating_another() -> None:
    client, emulator = _macro_client()
    _with_label(emulator)
    first = _macro(client).json()["data"]["macro"]["id"]
    again = _macro(client)
    assert again.status_code == 200 and again.json()["data"] == {"macro": {**again.json()["data"]["macro"], "id": first}, "created": False}
    assert len(emulator.created_macros) == 1


def test_the_label_has_to_exist_first() -> None:
    client, _ = _macro_client()
    response = _macro(client, "no-existe")
    assert response.status_code == 404 and response.json()["detail"] == "chatwoot_label_not_found"


@pytest.mark.parametrize("label", ["automation_paused", "cliente"])
def test_reserved_labels_do_not_get_a_macro(label: str) -> None:
    client, emulator = _macro_client()
    _with_label(emulator, label)
    response = _macro(client, label)
    assert response.status_code == 422 and response.json()["detail"] == "chatwoot_label_reserved"
    assert not any(method == "POST" for method, _, _ in emulator.requests)


def test_when_chatwoot_refuses_the_macro_the_agent_is_told_so() -> None:
    client, emulator = _macro_client()
    _with_label(emulator)
    emulator.macro_reject = 422
    response = _macro(client)
    assert response.status_code == 503 and response.json()["detail"] == "chatwoot_macro_rejected"
