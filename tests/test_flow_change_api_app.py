"""Las rutas de cambio del Automation Expert (src/bridge/flow_change_api.py) sobre el manifiesto de ATT1.

El almacen no se reemplaza con un falso que conteste lo que la prueba quiere:
la app usa el cliente real (``FlowChangeStore``) y PostgREST se emula con
``httpx.MockTransport``, con las reglas de los envoltorios de 20261021000100
que importan aca (la base del borrador, la revision, el tope, la clase de quien
pide) y los errores con la forma exacta que contesto PostgREST v14.15 el
2026-10-21 (tests/fixtures/automation_expert_change/). Lo que la base hace de
verdad lo prueba PGlite (validate_automation_expert_change.mjs).

Fixtures: las definiciones de ATT1 del 2026-10-16
(tests/fixtures/flow_store/att1_definitions_20261016.json), publicadas como
v1, y el catalogo de la bandeja 11 leido de Chatwoot el 2026-10-21
(tests/fixtures/flow_change_operator/).
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from bridge import flow_change_api
from bridge.app import Settings, create_app
from bridge.instance_manifest import InstanceManifest

FIXTURES = Path(__file__).parent / "fixtures"
ATT1 = FIXTURES / "instances" / "att1"
TOKEN = "c" * 40
READ_TOKEN = "e" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SLACK = {"slack_user": "U0123456789"}
PREFIX = "/internal/automation-expert/v1"
CAPTURED = json.loads((FIXTURES / "automation_expert_change" /
                       "postgrest_responses_automation_expert_change_20261021.json").read_text())["responses"]
DEFINITIONS = {d["flujo"]: d for d in json.loads(
    (FIXTURES / "flow_store/att1_definitions_20261016.json").read_text())["definitions"]}
CATALOG = json.loads((FIXTURES / "flow_change_operator/chatwoot_db_message_templates_inbox_11_20261021.json")
                     .read_text())
# La historia: la captura v3 de ATT1 del 21/10 (la de la T2.2), con la forma que devuelve
# read_flow_engine_history_v1.
HISTORY = json.loads((FIXTURES / "flow_engine/att1_recovery_capture_v3_20261021.json").read_text())
KEYS = ("carrito", "pago_fallido", "precheckout")
IDS = {key: f"0000000{index}-0000-4000-8000-0000000a7701" for index, key in enumerate(KEYS, start=1)}


def _jwt(role: str) -> str:
    part = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{part({'alg': 'HS256'})}.{part({'role': role})}.firma"


DB_KEY = _jwt("automation_expert_change")
SERVICE_KEY = _jwt("service_role")


def _error(name: str) -> httpx.Response:
    captured = CAPTURED[name]
    return httpx.Response(captured["status"], json=captured["body"])


class PostgrestEmulator:
    """Los cuatro envoltorios, con las reglas que la prueba necesita y los errores capturados."""

    def __init__(self) -> None:
        self.flows = []
        for key in KEYS:
            definition = copy.deepcopy(DEFINITIONS[key])
            definition.update(version=1, estado="publicada")
            self.flows.append({"flow_id": IDS[key], "product_ref": "att1", "flow_key": key, "state": "prendido",
                               "active_version": 1, "generation": 3, "changed_at": "2026-10-21T16:00:00Z",
                               "changed_by_kind": "operador", "versions": {"1": definition}})
        self.proposals: dict[str, dict] = {}
        self.requests: list[tuple[str, dict, dict]] = []
        self.fail_with: str | None = None
        # Las dos lecturas del motor (flow-engine-v1, secciones 1 y 2): sin ellas, como una base sin la
        # migracion del motor, PostgREST contesta 403 y la simulacion queda failed.
        self.engine = True

    def _engine_read(self) -> dict:
        flows = []
        for flow in self.flows:
            definition = flow["versions"]["1"]
            flows.append({key: flow[key] for key in ("flow_id", "product_ref", "flow_key", "state",
                                                     "active_version", "generation")}
                         | {"route": {"executor": "legado", "legacy_kind": flow["flow_key"],
                                      "legacy_scope_key": "att1-recuperacion", "generation": 0,
                                      "changed_at": "2026-10-21T16:00:00Z"},
                            "versions": [{"version": 1, "definition": definition, "definition_sha256": "0" * 64,
                                          "published_at": "2026-10-21T16:00:00Z", "in_flight": 0,
                                          "pool": (definition.get("topes") or {}).get("bolsa")
                                          or "att1-recuperacion"}]})
        return {"contract": "flow-engine-v1", "contract_version": "1.0.0", "database_at": "2026-10-21T17:16:07Z",
                "engine": {"capabilities": "v1", "rules_version": 2}, "flows": flows, "next": None}

    def _draft_base(self, items: list[dict]) -> dict:
        by_id = {flow["flow_id"]: flow for flow in self.flows}
        return {item["flow_id"]: {"active_version": by_id[item["flow_id"]]["active_version"]} for item in items}

    def handler(self, request: httpx.Request) -> httpx.Response:
        function = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        self.requests.append((function, body, dict(request.headers)))
        if self.fail_with is not None:
            return _error(self.fail_with)
        if body.get("p_instance") != {"tenant_ref": "lancemos", "ally_ref": "att1"}:
            return _error("instance_not_bound_22023")
        if function == "read_flow_engine_v1" and self.engine:
            return httpx.Response(200, json=self._engine_read())
        if function == "read_flow_engine_history_v1" and self.engine:
            return httpx.Response(200, json=copy.deepcopy(HISTORY))
        if function == "read_flow_store_v1":
            return httpx.Response(200, json={"database_at": "2026-10-21T16:00:00Z", "products": ["att1"],
                                             "flows": copy.deepcopy(self.flows)})
        if function == "save_flow_change_draft_v1":
            if not body["p_items"]:
                return _error("save_items_invalid_22023")
            if body["p_base"] != self._draft_base(body["p_items"]):
                return httpx.Response(200, json={"ok": False, "reason_code": "flow_change_stale_base"})
            if body["p_proposal_id"] is None:
                proposal_id = str(uuid4())
                self.proposals[proposal_id] = {
                    "proposal_id": proposal_id, "status": "borrador", "effective_status": "borrador",
                    "revision": 1, "items": body["p_items"], "draft_base": body["p_base"],
                    "purpose": body["p_purpose"], "requested_by_kind": "automation_expert",
                    "requested_by_ref": body["p_requested_by_ref"], "validation": None, "events": []}
            else:
                proposal_id = body["p_proposal_id"]
                proposal = self.proposals.get(proposal_id)
                if proposal is None:
                    return _error("get_proposal_not_found_P0002")
                if proposal["revision"] != body["p_expected_revision"]:
                    return httpx.Response(200, json={"ok": False, "reason_code": "flow_change_revision_mismatch",
                                                     "revision": proposal["revision"]})
                proposal.update(revision=proposal["revision"] + 1, items=body["p_items"], validation=None)
            proposal = self.proposals[proposal_id]
            return httpx.Response(200, json={"ok": True, "proposal_id": proposal_id,
                                             "revision": proposal["revision"], "status": "borrador"})
        if function == "get_flow_change_proposal_v1":
            proposal = self.proposals.get(body["p_proposal_id"])
            if proposal is None:
                return _error("get_proposal_not_found_P0002")
            return httpx.Response(200, json=copy.deepcopy(proposal))
        if function == "record_flow_change_validation_v1":
            proposal = self.proposals.get(body["p_proposal_id"])
            if proposal is None:
                return _error("get_proposal_not_found_P0002")
            if proposal["revision"] != body["p_revision"]:
                return httpx.Response(200, json={"ok": False, "reason_code": "flow_change_revision_mismatch"})
            proposal["validation"] = {"validator": body["p_validator"], "result": body["p_result"],
                                      "preview": body["p_preview"]}
            if body["p_result"]["blocks"]:
                return httpx.Response(200, json={"ok": True, "status": "borrador",
                                                 "blocks": len(body["p_result"]["blocks"])})
            fingerprint = hashlib.sha256(json.dumps(proposal, sort_keys=True).encode()).hexdigest()
            proposal.update(status="validada", effective_status="validada", fingerprint=fingerprint,
                            expires_at="2026-10-22T16:00:00Z")
            return httpx.Response(200, json={"ok": True, "status": "validada", "fingerprint": fingerprint,
                                             "expires_at": "2026-10-22T16:00:00Z"})
        return _error("publish_denied_42501")


class FakeChatwoot:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.inboxes: list[int] = []

    async def get_inbox(self, *, inbox_id: int) -> dict:
        self.inboxes.append(inbox_id)
        if self.failure is not None:
            raise self.failure
        return {"id": inbox_id, "channel_type": "Channel::Whatsapp",
                "message_templates": copy.deepcopy(CATALOG["message_templates"])}


class SharedSupabaseSpy:
    """El cliente de service_role: las rutas de cambio no lo usan nunca."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def resolve_commercial_ally_runtime_binding(self, config: object) -> object:
        return config

    def __getattr__(self, name: str):  # cualquier otra llamada queda registrada
        async def record(*args: object, **kwargs: object) -> object:
            self.calls.append(name)
            raise AssertionError(f"the change routes used service_role: {name}")

        return record


def _settings(**overrides: object) -> Settings:
    manifest = InstanceManifest.from_toml_file(ATT1 / "instancia.toml")
    settings = Settings(
        webhook_secret="test-secret", allowed_jid=None,
        capture_dir=Path("/tmp/automation-expert-change-captures"), max_age_seconds=300,
        commercial_ally_config=manifest.to_commercial_ally_config(),
        commercial_ally_manifest_path=Path("/instancia/instancia.toml"),
        instance_manifest=manifest, hermes_model_name=manifest.agent_model_name, agent_bot_id=2,
        chatwoot_base_url="https://chatwoot.example.org", agent_provenance_scope_ref="att1-agent-bot",
        pilot_boundary_enabled=False, pilot_scope_key="att1-recuperacion", pilot_scope_version=5,
        pilot_precheckout_scope_key="att1-primer-contacto", pilot_precheckout_scope_version=2,
        automation_expert_read_enabled=True, automation_expert_read_token=READ_TOKEN,
        supabase_base_url="https://att1-rest.internal", supabase_service_role_key=SERVICE_KEY,
        automation_expert_change_enabled=True, automation_expert_change_token=TOKEN,
        automation_expert_change_db_key=DB_KEY,
    )
    return replace(settings, **overrides)


def _client(emulator: PostgrestEmulator | None = None, chatwoot: FakeChatwoot | None = None,
            spy: SharedSupabaseSpy | None = None, **overrides: object) -> tuple[TestClient, PostgrestEmulator]:
    emulator = emulator or PostgrestEmulator()
    store = flow_change_api.FlowChangeStore(base_url="https://att1-rest.internal", db_key=DB_KEY,
                                            transport=httpx.MockTransport(emulator.handler))
    app = create_app(_settings(**overrides), supabase_client=spy or SharedSupabaseSpy(),  # type: ignore[arg-type]
                     chatwoot_client=chatwoot or FakeChatwoot(), flow_change_store=store)  # type: ignore[arg-type]
    return TestClient(app), emulator


def _cart_wait_item(wait: str = "2h") -> dict:
    definition = copy.deepcopy(DEFINITIONS["carrito"])
    definition["pasos"] = [{"esperar": wait}, *definition["pasos"]]
    return {"producto": "att1", "flujo": "carrito", "flow_id": IDS["carrito"], "tipo": "version",
            "definicion": definition, "en_curso": "seguir"}


def _propose_body(**overrides: object) -> dict:
    body = {"items": [_cart_wait_item()], "base": {IDS["carrito"]: {"active_version": 1}},
            "purpose": "que el carrito espere 2 horas antes del primer mensaje", "requested_by": SLACK}
    body.update(overrides)
    return body


# ----------------------------------------------------------------- el flag y las claves


def test_with_the_flag_off_the_routes_do_not_exist_and_ready_does_not_name_them() -> None:
    client, _ = _client(automation_expert_change_enabled=False, automation_expert_change_token=None,
                        automation_expert_change_db_key=None)
    assert client.post(f"{PREFIX}/flow-changes", headers=AUTH, json=_propose_body()).status_code == 404
    assert "automation_expert_change" not in json.dumps(client.get("/ready").json())


def test_ready_says_enabled_and_without_the_token_it_is_401() -> None:
    client, emulator = _client()
    assert "automation_expert_change" in json.dumps(client.get("/ready").json())
    response = client.post(f"{PREFIX}/flow-changes", json=_propose_body())
    assert response.status_code == 401
    assert response.json()["detail"] == "automation_expert_change_authentication_required"
    assert response.headers["www-authenticate"] == "Bearer"
    # La clave de lectura no abre las rutas de cambio.
    wrong = client.post(f"{PREFIX}/flow-changes", json=_propose_body(),
                        headers={"Authorization": f"Bearer {READ_TOKEN}"})
    assert wrong.status_code == 401 and emulator.requests == []


@pytest.mark.parametrize(("overrides", "message"), [
    ({"automation_expert_change_token": "corta"}, "at least 32 characters"),
    ({"automation_expert_change_token": READ_TOKEN}, "must differ from every other secret"),
    ({"automation_expert_change_db_key": "no-es-un-jwt"}, "role is automation_expert_change"),
    ({"automation_expert_change_db_key": SERVICE_KEY}, "role is automation_expert_change"),
    ({"automation_expert_read_enabled": False, "automation_expert_read_token": None},
     "requires AUTOMATION_EXPERT_READ_ENABLED"),
    ({"supabase_base_url": None}, "requires SUPABASE_BASE_URL"),
])
def test_the_bridge_does_not_start_with_a_wrong_configuration(overrides: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _client(**overrides)


def test_the_db_key_cannot_be_the_service_role_key_even_if_it_says_the_role() -> None:
    with pytest.raises(ValueError, match="must not be the service_role key"):
        _client(supabase_service_role_key=DB_KEY)


# ----------------------------------------------------------------- proponer


def test_proposing_a_wait_in_the_cart_saves_the_normalized_draft_with_the_role_key() -> None:
    spy = SharedSupabaseSpy()
    client, emulator = _client(spy=spy)
    response = client.post(f"{PREFIX}/flow-changes", headers=AUTH, json=_propose_body())
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["contract"], body["contract_version"]) == ("flow-change-cycle-v1", "1.4.0")
    assert body["instance"] == {"tenant_ref": "lancemos", "ally_ref": "att1"}
    assert body["data"]["status"] == "borrador" and body["data"]["revision"] == 1
    [read, save] = emulator.requests
    assert (read[0], save[0]) == ("read_flow_store_v1", "save_flow_change_draft_v1")
    # La clave del rol, nunca la de service_role, y el pedido del agente tal cual.
    for _, _, headers in emulator.requests:
        assert headers["authorization"] == f"Bearer {DB_KEY}" and headers["apikey"] == DB_KEY
    assert save[1]["p_requested_by_ref"] == "U0123456789"
    assert save[1]["p_base"] == {IDS["carrito"]: {"active_version": 1}}
    assert save[1]["p_items"][0]["definicion"]["pasos"][0] == {"esperar": "2h"}
    assert spy.calls == []


def test_a_draft_with_blocks_is_not_saved() -> None:
    item = _cart_wait_item()
    item["definicion"]["horario"] = {"zona": "Marte/Olympus", "desde": "09:00", "hasta": "21:00"}
    client, emulator = _client()
    response = client.post(f"{PREFIX}/flow-changes", headers=AUTH, json=_propose_body(items=[item]))
    assert response.status_code == 422
    assert response.json()["detail"] == "flow_change_draft_blocked"
    assert response.json()["data"]["blocks"]
    assert [function for function, _, _ in emulator.requests] == ["read_flow_store_v1"]


def test_a_purpose_with_a_phone_or_a_mail_is_refused_before_the_store() -> None:
    client, emulator = _client()
    for purpose in ("que le escriba al 55 1234 5678", "avisarle a ana@example.test"):
        response = client.post(f"{PREFIX}/flow-changes", headers=AUTH, json=_propose_body(purpose=purpose))
        assert (response.status_code, response.json()["detail"]) == (422, "identifier_not_allowed")
    assert emulator.requests == []


@pytest.mark.parametrize("body", [
    _propose_body(requested_by={"slack_user": "dan"}),
    _propose_body(requested_by={"slack_user": "U0123456789", "name": "dan"}),
    _propose_body(extra=True),
    _propose_body(proposal_id=str(uuid4())),
    _propose_body(purpose=""),
    _propose_body(items=[]),
])
def test_a_body_that_does_not_fit_is_a_closed_code(body: dict) -> None:
    client, emulator = _client()
    response = client.post(f"{PREFIX}/flow-changes", headers=AUTH, json=body)
    assert response.status_code == 422
    assert response.json()["detail"] in ("invalid_body", "flow_change_items_invalid")
    assert emulator.requests == []


def test_a_body_over_256_kib_is_refused() -> None:
    client, emulator = _client()
    response = client.post(f"{PREFIX}/flow-changes", headers=AUTH,
                           content=b'{"x": "' + b"a" * (300 * 1024) + b'"}')
    assert (response.status_code, response.json()["detail"]) == (422, "body_too_large")
    assert emulator.requests == []


def test_a_stale_base_is_caught_by_the_validator_or_by_the_base() -> None:
    # Vieja al leer: la ataja el validador, y no se guarda.
    client, emulator = _client()
    response = client.post(f"{PREFIX}/flow-changes", headers=AUTH,
                           json=_propose_body(base={IDS["carrito"]: {"active_version": 2}}))
    assert response.status_code == 422
    assert "flow_change_stale_base" in [b["code"] for b in response.json()["data"]["blocks"]]
    assert [function for function, _, _ in emulator.requests] == ["read_flow_store_v1"]
    # Cambia entre la lectura y el guardado: la ataja la base, 409.
    emulator = PostgrestEmulator()
    original = emulator.handler

    def publish_in_between(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("save_flow_change_draft_v1"):
            emulator.flows[0]["active_version"] = None
        return original(request)

    emulator.handler = publish_in_between  # type: ignore[method-assign]
    client, _ = _client(emulator)
    response = client.post(f"{PREFIX}/flow-changes", headers=AUTH, json=_propose_body())
    assert (response.status_code, response.json()["detail"]) == (409, "flow_change_stale_base")


# ----------------------------------------------------------------- validar y releer


def _proposed(client: TestClient) -> str:
    response = client.post(f"{PREFIX}/flow-changes", headers=AUTH, json=_propose_body())
    assert response.status_code == 201, response.text
    return response.json()["data"]["proposal_id"]


def test_validating_freezes_the_proposal_with_the_preview_and_the_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    # La identidad del validador sale de la imagen (el Dockerfile pone SETTER_VERSION y GIT_SHA).
    monkeypatch.setenv("SETTER_VERSION", "1.15.5")
    monkeypatch.setenv("GIT_SHA", "b163669f57bfdafbac6cb19d82dd357c83c3c1e8")
    chatwoot = FakeChatwoot()
    client, emulator = _client(chatwoot=chatwoot)
    proposal_id = _proposed(client)
    response = client.post(f"{PREFIX}/flow-changes/{proposal_id}/validate", headers=AUTH,
                           json={"revision": 1, "requested_by": SLACK})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "validada" and len(data["fingerprint"]) == 64
    assert data["blocks"] == [] and [w["code"] for w in data["warnings"]] == ["wait_without_contesta"]
    assert {"ruta": "/pasos/0/esperar", "antes": None, "despues": "2h"} in data["preview"]["cambios"][0]["diff"]
    # La simulacion de la espera, sobre la historia de ATT1: queda congelada en la vista previa.
    simulation = data["preview"]["simulacion"]
    assert simulation["status"] == "built" and len(simulation["summary_sha256"]) == 64
    assert simulation["summary"]["cambios"] > 0
    assert "simulation_not_built" not in [gap["code"] for gap in data["preview"]["huecos"]]
    history = [body for function, body, _ in emulator.requests if function == "read_flow_engine_history_v1"]
    assert history[0]["p_product_ref"] == "att1"
    since = datetime.fromisoformat(history[0]["p_since"].replace("Z", "+00:00"))
    assert timedelta(days=7, hours=23) < datetime.now(UTC) - since < timedelta(days=8)
    assert chatwoot.inboxes == [11]
    record = [body for function, body, _ in emulator.requests if function == "record_flow_change_validation_v1"]
    assert record[0]["p_validator"]["actor"] == "automation_expert"
    assert record[0]["p_validator"]["phase"] == "publish"
    assert record[0]["p_validator"]["product_version"] == "1.15.5"
    assert record[0]["p_requested_by_ref"] == "U0123456789"
    reread = client.get(f"{PREFIX}/flow-changes/{proposal_id}", headers=AUTH)
    assert reread.status_code == 200
    assert reread.json()["data"]["status"] == "validada"
    assert reread.json()["data"]["preview"]["simulacion"]["summary_sha256"] == simulation["summary_sha256"]
    assert "validation" not in reread.json()["data"]


def test_without_the_catalog_it_does_not_validate() -> None:
    client, emulator = _client(chatwoot=FakeChatwoot(failure=RuntimeError("chatwoot caido")))
    proposal_id = _proposed(client)
    response = client.post(f"{PREFIX}/flow-changes/{proposal_id}/validate", headers=AUTH,
                           json={"revision": 1, "requested_by": SLACK})
    assert (response.status_code, response.json()["detail"]) == (503, "template_catalog_unavailable")
    assert "record_flow_change_validation_v1" not in [function for function, _, _ in emulator.requests]


def test_another_revision_is_409() -> None:
    client, _ = _client()
    proposal_id = _proposed(client)
    response = client.post(f"{PREFIX}/flow-changes/{proposal_id}/validate", headers=AUTH,
                           json={"revision": 2, "requested_by": SLACK})
    assert (response.status_code, response.json()["detail"]) == (409, "flow_change_revision_mismatch")


def test_a_proposal_that_does_not_exist_is_404_although_postgrest_says_500() -> None:
    client, _ = _client()
    response = client.get(f"{PREFIX}/flow-changes/{uuid4()}", headers=AUTH)
    assert (response.status_code, response.json()["detail"]) == (404, "flow_change_not_found")
    assert client.get(f"{PREFIX}/flow-changes/no-es-uuid", headers=AUTH).json()["detail"] == "invalid_proposal_id"


def test_a_permission_denied_or_an_unknown_error_is_unavailable_and_says_nothing_else() -> None:
    emulator = PostgrestEmulator()
    emulator.fail_with = "publish_denied_42501"
    client, _ = _client(emulator)
    response = client.post(f"{PREFIX}/flow-changes", headers=AUTH, json=_propose_body())
    assert (response.status_code, response.json()["detail"]) == (503, "automation_expert_change_unavailable")
    assert "permission" not in response.text


def test_without_the_image_identity_it_does_not_validate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SETTER_VERSION", raising=False)
    monkeypatch.delenv("GIT_SHA", raising=False)
    client, emulator = _client()
    proposal_id = _proposed(client)
    response = client.post(f"{PREFIX}/flow-changes/{proposal_id}/validate", headers=AUTH,
                           json={"revision": 1, "requested_by": SLACK})
    assert (response.status_code, response.json()["detail"]) == (503, "automation_expert_change_unavailable")
    assert "record_flow_change_validation_v1" not in [function for function, _, _ in emulator.requests]


def test_the_flow_store_gives_each_flow_with_its_active_definition_with_the_role_key() -> None:
    client, emulator = _client()
    response = client.get(f"{PREFIX}/flow-store", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["contract"] == "flow-change-cycle-v1"
    flows = {flow["flow_key"]: flow for flow in body["data"]["flows"]}
    assert set(flows) == set(KEYS)
    cart = flows["carrito"]
    assert (cart["flow_id"], cart["active_version"], cart["versions"]) == (IDS["carrito"], 1, [1])
    assert cart["active_definition"] == emulator.flows[0]["versions"]["1"]
    # Lee con la clave del rol, nunca con la de service_role.
    function, _, headers = emulator.requests[0]
    assert function == "read_flow_store_v1"
    assert headers["authorization"] == f"Bearer {DB_KEY}"
    assert client.get(f"{PREFIX}/flow-store").status_code == 401


def test_without_the_engine_functions_it_validates_and_the_simulation_says_why(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SETTER_VERSION", "1.15.5")
    monkeypatch.setenv("GIT_SHA", "b163669f57bfdafbac6cb19d82dd357c83c3c1e8")
    emulator = PostgrestEmulator()
    emulator.engine = False
    client, _ = _client(emulator)
    proposal_id = _proposed(client)
    response = client.post(f"{PREFIX}/flow-changes/{proposal_id}/validate", headers=AUTH,
                           json={"revision": 1, "requested_by": SLACK})
    assert response.status_code == 200, response.text
    preview = response.json()["data"]["preview"]
    assert preview["simulacion"] == {"status": "failed", "reason_code": "flow_engine_unavailable"}
    assert {"code": "simulation_failed", "reason_code": "flow_engine_unavailable"} in preview["huecos"]
