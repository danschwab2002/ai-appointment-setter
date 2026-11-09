"""template-request-v1 (F-146, parte 2): las rutas del Automation Expert para las solicitudes de plantilla.

Sobre el arnes de tests/test_flow_change_api_app.py (la app con el cliente real
del almacen y PostgREST emulado). Las respuestas de la base salen de la salida
real de las RPC de 20261102000200 sobre la cadena de migraciones en PGlite
(tests/fixtures/meta_template_requests/rpc_outputs.json, de
validate_meta_template_requests.mjs --emit-fixture). El catalogo es el de la
bandeja 11 de ATT1 leido de Chatwoot el 2026-10-21, y las plantillas, las
reales de ATT1 (plantillas_reales_att1_maribel_20261102.json).
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from bridge import flow_change_api, template_request_api
from bridge.app import create_app
from bridge.template_request import meta_request, parse_template
from test_flow_change_api_app import AUTH, PREFIX, SLACK, FakeChatwoot, PostgrestEmulator, SharedSupabaseSpy
from test_flow_change_api_app import DB_KEY as AE_DB_KEY
from test_flow_change_api_app import _settings as _change_settings

FIXTURES = Path(__file__).parent / "fixtures"
OUTPUTS = json.loads((FIXTURES / "meta_template_requests" / "rpc_outputs.json").read_text(encoding="utf-8"))
REAL = {row["template"]["nombre"]: row["template"] for row in json.loads(
    (FIXTURES / "meta_template_requests" / "plantillas_reales_att1_maribel_20261102.json").read_text(
        encoding="utf-8"))["plantillas"]}


def _jwt(role: str) -> str:
    part = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{part({'alg': 'RS256', 'kid': 'k'})}.{part({'role': role})}.firma"


APPROVAL = {
    "flow_approval_enabled": True, "flow_approval_base_url": "https://setter-att1.example.test",
    "flow_approval_slack_team_id": "T0LANCEMOS01", "flow_approval_slack_approvers": "U0DANSCHWAB1",
    "flow_approval_slack_client_id": "1234567890.987654321", "flow_approval_slack_client_secret": "s" * 40,
    "flow_approval_session_secret": "k" * 48, "flow_change_approver_db_key": _jwt("flow_change_approver"),
    "meta_templates_access_token": "EAAtoken-solo-de-plantillas-" + "y" * 40, "meta_waba_id": "1234567890123",
}


def _template(name: str = "att1_pago_efectivo_confirmado_02") -> dict:
    """El texto real de att1_pago_efectivo_confirmado_01 (aprobada en Meta), con el numero siguiente."""
    template = copy.deepcopy(REAL["att1_pago_efectivo_confirmado_01"])
    template["nombre"] = name
    return template


def _body(**overrides: object) -> dict:
    body = {"template": _template(), "purpose": "avisar que se confirmo el pago en efectivo", "requested_by": SLACK}
    body.update(overrides)
    return body


class TemplateEmulator(PostgrestEmulator):
    """El emulador del arnes, con las cuatro RPC del agente tal como contestan en la base (rpc_outputs.json)."""

    def __init__(self) -> None:
        super().__init__()
        self.template_requests: dict[str, dict] = {}
        self.save_result: dict | None = None

    def _stored(self, request_id: str, body: dict) -> dict:
        stored = copy.deepcopy(OUTPUTS["got"])
        draft = body["p_draft"]
        stored.update(request_id=request_id, status="borrador", effective_status="borrador", revision=1,
                      template_name=draft["nombre"], language=draft["idioma"], category_requested=draft["categoria"],
                      draft=draft, purpose=body["p_purpose"], proposal_id=body["p_proposal_id"],
                      step_pointer=body["p_step_pointer"], validation=None, fingerprint=None, validated_at=None,
                      expires_at=None)
        return stored

    def handler(self, request: httpx.Request) -> httpx.Response:
        function = request.url.path.rsplit("/", 1)[-1]
        if "meta_template" not in function:
            return super().handler(request)
        body = json.loads(request.content)
        self.requests.append((function, body, dict(request.headers)))
        if body.get("p_instance") != {"tenant_ref": "lancemos", "ally_ref": "att1"}:
            return httpx.Response(400, json={"code": "22023", "message": "flow_store_instance_not_bound"})
        if function in ("save_meta_template_request_draft_v1", "save_meta_template_request_draft_v2"):
            if self.save_result is not None:
                return httpx.Response(200, json=self.save_result)
            request_id = body["p_request_id"] or str(uuid4())
            if body["p_request_id"] is None:
                self.template_requests[request_id] = self._stored(request_id, body)
            else:
                stored = self.template_requests[request_id]
                if stored["revision"] != body["p_expected_revision"]:
                    return httpx.Response(200, json={"ok": False, "reason_code": "meta_template_revision_mismatch"})
                stored.update(revision=stored["revision"] + 1, draft=body["p_draft"])
            saved = {**OUTPUTS["saved"], "request_id": request_id,
                     "revision": self.template_requests[request_id]["revision"]}
            return httpx.Response(200, json=saved)
        if function == "get_meta_template_request_v1":
            stored = self.template_requests.get(body["p_request_id"])
            if stored is None:
                return httpx.Response(500, json={"code": "P0002", "message": "meta_template_request_not_found"})
            return httpx.Response(200, json=copy.deepcopy(stored))
        if function == "record_meta_template_request_validation_v1":
            stored = self.template_requests[body["p_request_id"]]
            stored["validation"] = {"validator": body["p_validator"], "result": body["p_result"],
                                    "preview": body["p_preview"], "meta_request": body["p_meta_request"]}
            if body["p_result"]["blocks"]:
                return httpx.Response(200, json={"ok": True, "status": "borrador",
                                                 "blocks": len(body["p_result"]["blocks"])})
            stored.update(status="validada", effective_status="validada")
            return httpx.Response(200, json=OUTPUTS["recorded"])
        if function == "list_meta_template_requests_v1":
            listed = copy.deepcopy(OUTPUTS["listed_open" if body["p_only_open"] else "listed_all"])
            listed["requests"] = listed["requests"][: body["p_limit"]]
            return httpx.Response(200, json=listed)
        return httpx.Response(403, json={"code": "42501", "message": "permission denied"})


def _client(emulator: TemplateEmulator | None = None, chatwoot: FakeChatwoot | None = None,
            **overrides: object) -> tuple[TestClient, TemplateEmulator]:
    emulator = emulator or TemplateEmulator()
    transport = httpx.MockTransport(emulator.handler)
    settings = _change_settings(**{"meta_templates_enabled": True, **APPROVAL, **overrides})
    app = create_app(
        settings, supabase_client=SharedSupabaseSpy(),  # type: ignore[arg-type]
        chatwoot_client=chatwoot or FakeChatwoot(),  # type: ignore[arg-type]
        flow_change_store=flow_change_api.FlowChangeStore(base_url="https://att1-rest.internal", db_key=AE_DB_KEY,
                                                          transport=transport),
        template_request_store=template_request_api.TemplateRequestStore(
            base_url="https://att1-rest.internal", db_key=AE_DB_KEY, transport=transport),
    )
    return TestClient(app), emulator


@pytest.fixture(autouse=True)
def _image(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SETTER_VERSION", "v1.25.0")
    monkeypatch.setenv("GIT_SHA", "069e602e")


def _calls(emulator: TemplateEmulator, function: str) -> list[dict]:
    return [body for name, body, _ in emulator.requests if name == function]


# ------------------------------------------------------------------- el flag


def test_with_the_flag_off_the_routes_do_not_exist_and_ready_does_not_name_them() -> None:
    client, emulator = _client(meta_templates_enabled=False)
    assert client.post(f"{PREFIX}/template-requests", headers=AUTH, json=_body()).status_code == 404
    assert client.get(f"{PREFIX}/template-requests", headers=AUTH).status_code == 404
    assert "meta_templates" not in json.dumps(client.get("/ready").json())
    assert emulator.requests == []


def test_ready_says_enabled_and_without_the_token_it_is_401() -> None:
    client, _ = _client()
    assert '"meta_templates": "enabled"' in json.dumps(client.get("/ready").json())
    response = client.post(f"{PREFIX}/template-requests", json=_body())
    assert (response.status_code, response.json()["detail"]) == (401, "automation_expert_change_authentication_required")
    assert client.get(f"{PREFIX}/template-requests",
                      headers={"Authorization": "Bearer " + "e" * 40}).status_code == 401


@pytest.mark.parametrize(("overrides", "message"), [
    ({"flow_approval_enabled": False}, "META_TEMPLATES_ENABLED requires FLOW_APPROVAL_ENABLED"),
    ({"automation_expert_change_enabled": False}, "META_TEMPLATES_ENABLED requires AUTOMATION_EXPERT_CHANGE_ENABLED"),
])
def test_the_bridge_does_not_start_without_the_page_or_the_agent_routes(overrides: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _client(**overrides)


# ------------------------------------------------------------------- proponer


def test_proposing_saves_the_normalized_draft_with_the_role_key_and_answers_the_bubble() -> None:
    client, emulator = _client()
    response = client.post(f"{PREFIX}/template-requests", headers=AUTH, json=_body())
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["contract"], body["contract_version"]) == ("template-request-v1", "1.3.0")
    data = body["data"]
    assert data["status"] == "borrador" and data["revision"] == 1 and data["variables"] == ["nombre", "producto"]
    assert data["burbuja"]["cuerpo"].startswith("Hola, [nombre]. Se confirmó tu pago de [producto].")
    assert "Alimenta tu Tiroides" in data["burbuja"]["cuerpo_con_ejemplo"]
    [saved] = _calls(emulator, "save_meta_template_request_draft_v1")
    assert saved["p_draft"] == parse_template(_template()).to_json()
    assert (saved["p_proposal_id"], saved["p_step_pointer"], saved["p_requested_by_ref"]) == (None, None, SLACK["slack_user"])
    headers = next(h for name, _, h in emulator.requests if name == "save_meta_template_request_draft_v1")
    assert headers["apikey"] == AE_DB_KEY


def test_a_draft_with_blocks_is_not_saved_and_says_each_rule() -> None:
    client, emulator = _client()
    # La bienvenida real de ATT1, con su URL fija mal escrita.
    welcome = copy.deepcopy(REAL["att1_bienvenida_01"])
    welcome["nombre"] = "att1_bienvenida_02"
    next(b for b in welcome["botones"] if b["tipo"] == "url_fija")["url"] = "http://forms.gle/x"
    response = client.post(f"{PREFIX}/template-requests", headers=AUTH, json=_body(template=welcome))
    assert response.status_code == 422
    assert response.json()["detail"] == "template_request_draft_blocked"
    assert [b["code"] for b in response.json()["data"]["blocks"]] == ["template_fixed_url_invalid"]
    assert _calls(emulator, "save_meta_template_request_draft_v1") == []


def test_a_name_that_is_in_the_inbox_catalog_is_blocked() -> None:
    client, emulator = _client()
    response = client.post(f"{PREFIX}/template-requests", headers=AUTH,
                           json=_body(template=_template("att1_pago_efectivo_confirmado_01")))
    assert response.status_code == 422
    assert "template_name_in_catalog" in [b["code"] for b in response.json()["data"]["blocks"]]
    assert _calls(emulator, "save_meta_template_request_draft_v1") == []


def test_without_chatwoot_the_draft_is_saved_and_the_catalog_waits_for_the_validation() -> None:
    client, emulator = _client(chatwoot=FakeChatwoot(failure=httpx.ConnectError("caido")))
    assert client.post(f"{PREFIX}/template-requests", headers=AUTH, json=_body()).status_code == 201
    assert len(_calls(emulator, "save_meta_template_request_draft_v1")) == 1


@pytest.mark.parametrize(("body", "code", "field"), [
    (_body(purpose="llamar al 55 1234 5678"), "identifier_not_allowed", "purpose"),
    (_body(template={**_template(), "nombre": "Con-Mayus"}), "template_request_invalid", "template.nombre"),
    (_body(template={**_template(), "otra": 1}), "template_request_invalid", "template"),
    (_body(revision=1), "invalid_body", "revision"),
    (_body(for_flow={"proposal_id": "no", "step": "/items/0"}), "invalid_body", "for_flow.proposal_id"),
    (_body(for_flow={"proposal_id": str(uuid4())}), "invalid_body", "for_flow"),
    ({"template": _template(), "purpose": "x"}, "invalid_body", "body"),
])
def test_a_body_that_does_not_close_is_refused_before_the_base(body: dict, code: str, field: str) -> None:
    client, emulator = _client()
    response = client.post(f"{PREFIX}/template-requests", headers=AUTH, json=body)
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == code and response.json()["reason"]["field"] == field
    assert "Con-Mayus" not in response.text and "1234" not in response.text
    assert _calls(emulator, "save_meta_template_request_draft_v1") == []


def test_what_the_base_refuses_is_a_409_with_its_code() -> None:
    emulator = TemplateEmulator()
    emulator.save_result = {"ok": False, "reason_code": "meta_template_name_taken"}
    client, _ = _client(emulator)
    response = client.post(f"{PREFIX}/template-requests", headers=AUTH, json=_body())
    assert (response.status_code, response.json()["detail"]) == (409, "meta_template_name_taken")
    emulator.save_result = {"ok": False, "reason_code": "otra_cosa"}
    assert client.post(f"{PREFIX}/template-requests", headers=AUTH, json=_body()).status_code == 503


# ------------------------------------------------------------------- validar


def _proposed(client: TestClient, **overrides: object) -> str:
    response = client.post(f"{PREFIX}/template-requests", headers=AUTH, json=_body(**overrides))
    assert response.status_code == 201, response.text
    return response.json()["data"]["request_id"]


def test_validating_freezes_the_exact_meta_request_and_the_bubble() -> None:
    client, emulator = _client()
    request_id = _proposed(client)
    response = client.post(f"{PREFIX}/template-requests/{request_id}/validate", headers=AUTH,
                           json={"revision": 1, "requested_by": SLACK})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "validada" and data["blocks"] == [] and len(data["fingerprint"]) == 64
    [recorded] = _calls(emulator, "record_meta_template_request_validation_v1")
    assert recorded["p_validator"] == {"contract": "template-request-v1", "rules_version": 1,
                                       "product_version": "v1.25.0", "git_sha": "069e602e"}
    from test_template_request import ATT1

    assert recorded["p_meta_request"] == meta_request(parse_template(_template()), ATT1)
    assert recorded["p_meta_request"]["components"][0]["example"] == {"body_text": [["María", "Alimenta tu Tiroides"]]}
    assert recorded["p_preview"]["burbuja"] == data["preview"]["burbuja"]
    assert len(recorded["p_preview"]["catalog_sha256"]) == 64


def test_validating_looks_at_the_catalog_even_if_proposing_could_not() -> None:
    chatwoot = FakeChatwoot(failure=httpx.ConnectError("caido"))
    client, emulator = _client(chatwoot=chatwoot)
    # att1_pago_efectivo_confirmado_01 ya esta en la bandeja 11: al proponer no se pudo mirar.
    request_id = _proposed(client, template=_template("att1_pago_efectivo_confirmado_01"))
    chatwoot.failure = None
    response = client.post(f"{PREFIX}/template-requests/{request_id}/validate", headers=AUTH,
                           json={"revision": 1, "requested_by": SLACK})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "borrador" and "template_name_in_catalog" in [b["code"] for b in data["blocks"]]
    assert _calls(emulator, "record_meta_template_request_validation_v1")[-1]["p_meta_request"] is None


def test_without_the_manifest_the_flag_does_not_start() -> None:
    from types import SimpleNamespace

    with pytest.raises(ValueError, match="requires an instance manifest"):
        template_request_api.check_settings(SimpleNamespace(
            meta_templates_enabled=True, automation_expert_change_enabled=True, flow_approval_enabled=True,
            instance_manifest=None))


def test_a_request_that_is_not_a_draft_any_more_is_not_validated_again() -> None:
    client, emulator = _client()
    request_id = _proposed(client)
    emulator.template_requests[request_id]["status"] = "validada"
    response = client.post(f"{PREFIX}/template-requests/{request_id}/validate", headers=AUTH,
                           json={"revision": 1, "requested_by": SLACK})
    assert (response.status_code, response.json()["detail"]) == (409, "meta_template_not_draft")
    assert _calls(emulator, "record_meta_template_request_validation_v1") == []


def test_without_the_catalog_or_the_image_identity_it_does_not_validate(monkeypatch: pytest.MonkeyPatch) -> None:
    chatwoot = FakeChatwoot()
    client, emulator = _client(chatwoot=chatwoot)
    request_id = _proposed(client)
    chatwoot.failure = httpx.ConnectError("caido")
    response = client.post(f"{PREFIX}/template-requests/{request_id}/validate", headers=AUTH,
                           json={"revision": 1, "requested_by": SLACK})
    assert (response.status_code, response.json()["detail"]) == (503, "template_catalog_unavailable")
    monkeypatch.delenv("GIT_SHA")
    response = client.post(f"{PREFIX}/template-requests/{request_id}/validate", headers=AUTH,
                           json={"revision": 1, "requested_by": SLACK})
    assert (response.status_code, response.json()["detail"]) == (503, "automation_expert_change_unavailable")
    assert _calls(emulator, "record_meta_template_request_validation_v1") == []


def test_another_revision_is_409_and_one_that_does_not_exist_is_404() -> None:
    client, _ = _client()
    request_id = _proposed(client)
    response = client.post(f"{PREFIX}/template-requests/{request_id}/validate", headers=AUTH,
                           json={"revision": 2, "requested_by": SLACK})
    assert (response.status_code, response.json()["detail"]) == (409, "meta_template_revision_mismatch")
    response = client.post(f"{PREFIX}/template-requests/{uuid4()}/validate", headers=AUTH,
                           json={"revision": 1, "requested_by": SLACK})
    assert (response.status_code, response.json()["detail"]) == (404, "meta_template_request_not_found")
    assert client.get(f"{PREFIX}/template-requests/no-es-un-id", headers=AUTH).status_code == 422


def _proposal_waiting(emulator: TemplateEmulator, name: str) -> str:
    proposal_id = str(uuid4())
    emulator.proposals[proposal_id] = {
        "proposal_id": proposal_id, "status": "borrador", "revision": 1,
        "items": [{"producto": "att1", "flujo": "aviso_efectivo", "tipo": "nuevo",
                   "definicion": {"pasos": [{"plantilla": {"a_crear": name}, "idioma": "es_MX"}]}}]}
    return proposal_id


def test_a_request_for_a_flow_step_is_checked_against_the_step_that_waits_for_it() -> None:
    client, emulator = _client()
    proposal_id = _proposal_waiting(emulator, "att1_pago_efectivo_confirmado_02")
    request_id = _proposed(client, for_flow={"proposal_id": proposal_id, "step": "/items/0/definicion/pasos/0"})
    response = client.post(f"{PREFIX}/template-requests/{request_id}/validate", headers=AUTH,
                           json={"revision": 1, "requested_by": SLACK})
    assert response.status_code == 200 and response.json()["data"]["blocks"] == []
    # Otro paso, que no la espera: queda borrador con el bloqueo.
    other = _proposal_waiting(emulator, "att1_otra_plantilla_01")
    second = _proposed(client, template=_template("att1_pago_efectivo_confirmado_03"),
                       for_flow={"proposal_id": other, "step": "/items/0/definicion/pasos/0"})
    response = client.post(f"{PREFIX}/template-requests/{second}/validate", headers=AUTH,
                           json={"revision": 1, "requested_by": SLACK})
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "borrador"
    assert [b["code"] for b in response.json()["data"]["blocks"]] == ["template_flow_step_mismatch"]
    assert _calls(emulator, "record_meta_template_request_validation_v1")[-1]["p_meta_request"] is None


# ------------------------------------------------------------------- leer


def test_the_list_answers_the_open_ones_with_the_role_key_and_no_slack_id() -> None:
    client, emulator = _client()
    response = client.get(f"{PREFIX}/template-requests", headers=AUTH)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["estado"] == "abiertas" and data["open_limit"] == 10
    assert _calls(emulator, "list_meta_template_requests_v1")[-1]["p_only_open"] is True
    statuses = {row["effective_status"] for row in data["requests"]}
    assert statuses and not statuses & {"en_catalogo", "rechazada", "descartada", "vencida"}
    everything = client.get(f"{PREFIX}/template-requests?estado=todas&limit=50", headers=AUTH).json()["data"]
    assert {"en_catalogo", "rechazada", "descartada"} <= {row["status"] for row in everything["requests"]}
    assert any(row["recategorized"] for row in everything["requests"])
    assert "U0" not in json.dumps(everything)
    for query in ("estado=cerradas", "limit=0", "limit=51", "limit=dos"):
        assert client.get(f"{PREFIX}/template-requests?{query}", headers=AUTH).status_code == 422


def test_reading_one_gives_its_state_and_the_preview() -> None:
    client, _ = _client()
    request_id = _proposed(client)
    client.post(f"{PREFIX}/template-requests/{request_id}/validate", headers=AUTH,
                json={"revision": 1, "requested_by": SLACK})
    response = client.get(f"{PREFIX}/template-requests/{request_id}", headers=AUTH)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["request_id"] == request_id and data["template_name"] == "att1_pago_efectivo_confirmado_02"
    assert data["preview"]["burbuja"]["categoria"] == "UTILITY" and data["result"] == {"blocks": [], "warnings": []}
    assert "requested_by_ref" not in data and "approved_by_ref" not in data


# ------------------------------------------------------------------- el estado ya cruzado con el catalogo
# La plantilla real de ATT1 esta en el catalogo capturado: APPROVED y UTILITY, con el mismo cuerpo que el borrador.
SENT_NAME = "att1_pago_efectivo_confirmado_01"


def _sent(emulator: TemplateEmulator, *, name: str = SENT_NAME, draft: dict | None = None, **row: object) -> str:
    request_id = str(uuid4())
    stored = copy.deepcopy(OUTPUTS["got"])
    template = draft or parse_template(copy.deepcopy(REAL[SENT_NAME])).to_json()
    stored.update(request_id=request_id, status="en_revision", effective_status="en_revision", template_name=name,
                  language="es_MX", category_requested="UTILITY", draft=template, send_attempts=1,
                  last_send_error=None, sent_at="2026-11-02T12:00:00+00:00", meta_status="PENDING")
    stored.update(row)
    emulator.template_requests[request_id] = stored
    return request_id


def _read(client: TestClient, request_id: str) -> dict:
    response = client.get(f"{PREFIX}/template-requests/{request_id}", headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_reading_one_crosses_the_catalog_in_the_bridge() -> None:
    client, emulator = _client()
    data = _read(client, _sent(emulator))
    # La solicitud sigue diciendo en_revision, y el catalogo la trae aprobada: el estado resuelto es el del catalogo.
    assert data["status"] == "en_revision"
    assert data["resolved"] == {"state": "aprobada_lista", "catalog_checked": True, "catalog_status": "APPROVED",
                                "catalog_category": "UTILITY"}


def test_a_failed_send_is_not_in_review_and_says_why_in_words() -> None:
    client, emulator = _client()
    # El nombre no esta en el catalogo: Meta nunca la recibio.
    data = _read(client, _sent(emulator, name="att1_nunca_llego_01", status="enviada", effective_status="enviada",
                               last_send_error="meta:100", send_attempts=2))
    assert data["resolved"]["state"] == "no_llego_a_meta"
    assert (data["last_send_error"], data["send_attempts"]) == ("meta:100", 2)
    assert data["send_error_text"] == "Meta no aceptó el pedido tal como salió. Pedile al agente que revise la plantilla."


def test_a_template_approved_by_a_person_is_not_read_as_approved_by_meta() -> None:
    client, emulator = _client()
    # Aun con el mismo nombre aprobado en el catalogo: esta solicitud no salio, y el catalogo no le aplica.
    data = _read(client, _sent(emulator, status="aprobada", effective_status="aprobada", send_attempts=0))
    assert data["resolved"]["state"] == "aprobada_sin_mandar" and data["resolved"]["catalog_status"] is None
    assert data["send_error_text"] is None


def test_without_chatwoot_the_read_answers_without_confirming() -> None:
    client, emulator = _client(chatwoot=FakeChatwoot(failure=RuntimeError("chatwoot caido")))
    data = _read(client, _sent(emulator))
    assert data["resolved"]["state"] == "en_revision" and data["resolved"]["catalog_checked"] is False


def test_a_chatwoot_that_hangs_does_not_stop_the_read(monkeypatch: pytest.MonkeyPatch) -> None:
    class Hanging(FakeChatwoot):
        async def get_inbox(self, *, inbox_id: int) -> dict:
            await asyncio.sleep(0.5)  # contesta tarde, y con el catalogo: si no se cortara, se lo creeria
            return await super().get_inbox(inbox_id=inbox_id)

    monkeypatch.setattr(template_request_api, "CATALOG_READ_TIMEOUT_SECONDS", 0.01)
    client, emulator = _client(chatwoot=Hanging())
    assert _read(client, _sent(emulator))["resolved"]["catalog_checked"] is False


def test_the_catalog_is_not_read_for_what_never_reached_meta() -> None:
    chatwoot = FakeChatwoot()
    client, emulator = _client(chatwoot=chatwoot)
    data = _read(client, _sent(emulator, status="validada", effective_status="validada", send_attempts=0))
    assert data["resolved"]["state"] == "esperando_que_la_manden" and chatwoot.inboxes == []


class _ListEmulator(TemplateEmulator):
    """Como la base: la lista no trae ni el intento ni el borrador."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("list_meta_template_requests_v1"):
            body = json.loads(request.content)
            self.requests.append(("list_meta_template_requests_v1", body, dict(request.headers)))
            rows = [{key: value for key, value in row.items()
                     if key not in ("send_attempts", "last_send_error", "draft", "validation", "events")}
                    for row in self.template_requests.values()]
            return httpx.Response(200, json={"requests": rows, "open": len(rows), "open_limit": 10,
                                             "database_at": "2026-11-08T12:00:00Z"})
        return super().handler(request)


def _rows(client: TestClient) -> dict[str, dict]:
    response = client.get(f"{PREFIX}/template-requests?estado=todas", headers=AUTH)
    assert response.status_code == 200, response.text
    return {row["request_id"]: row for row in response.json()["data"]["requests"]}


def test_the_list_brings_the_send_error_and_the_resolved_state() -> None:
    emulator = _ListEmulator()
    client, _ = _client(emulator)
    failed = _sent(emulator, name="att1_nunca_llego_01", status="enviada", effective_status="enviada",
                   last_send_error="meta_unreachable", send_attempts=5)
    approved = _sent(emulator)
    waiting = _sent(emulator, name="att1_todavia_no_01")
    person = _sent(emulator, name="att1_una_persona_01", status="aprobada", effective_status="aprobada", send_attempts=0)
    rows = _rows(client)
    assert rows[failed]["resolved"]["state"] == "no_llego_a_meta_sin_reintentos"
    assert (rows[failed]["last_send_error"], rows[failed]["send_attempts"]) == ("meta_unreachable", 5)
    assert rows[failed]["send_error_text"].startswith("Meta no contestó")
    assert rows[approved]["resolved"]["state"] == "aprobada_lista"
    assert rows[waiting]["resolved"]["state"] == "en_revision"
    assert rows[person]["resolved"]["state"] == "aprobada_sin_mandar"
    assert "draft" not in rows[approved]  # el borrador se relee para comparar, y no viaja en la lista
    # Solo se relee lo que hace falta: la enviada y la que esta en el catalogo, no las otras dos.
    assert sorted(call["p_request_id"] for call in _calls(emulator, "get_meta_template_request_v1")) == sorted(
        [failed, approved])


def test_the_list_rereads_a_bounded_number_and_never_claims_what_it_did_not_read() -> None:
    emulator = _ListEmulator()
    client, _ = _client(emulator)
    ids = [_sent(emulator, name=f"att1_envio_{index:02}", status="enviada", effective_status="enviada")
           for index in range(template_request_api.LIST_DETAIL_MAX + 2)]
    rows = _rows(client)
    assert len(_calls(emulator, "get_meta_template_request_v1")) == template_request_api.LIST_DETAIL_MAX
    assert {rows[item]["resolved"]["state"] for item in ids} == {"mandandose", "sin_confirmar"}
    assert sum(rows[item]["resolved"]["state"] == "sin_confirmar" for item in ids) == 2


def test_a_list_reread_that_fails_stops_and_stays_unconfirmed() -> None:
    class Failing(_ListEmulator):
        def handler(self, request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("get_meta_template_request_v1"):
                self.requests.append(("get_meta_template_request_v1", json.loads(request.content), {}))
                return httpx.Response(503, json={"message": "caida"})
            return super().handler(request)

    emulator = Failing()
    client, _ = _client(emulator)
    ids = [_sent(emulator, name=f"att1_envio_{index:02}", status="enviada", effective_status="enviada")
           for index in range(3)]
    rows = _rows(client)
    assert {rows[item]["resolved"]["state"] for item in ids} == {"sin_confirmar"}
    assert len(_calls(emulator, "get_meta_template_request_v1")) == 1


# ------------------------------------------------------------------- el hilo (F-146 parte 4, E-361)


def test_the_slack_thread_is_saved_for_the_notice() -> None:
    client, db = _client()
    thread = {"channel": "D0123456789", "thread_ts": "1762700000.123456"}
    assert client.post(f"{PREFIX}/template-requests", json=_body(slack_thread=thread), headers=AUTH).status_code == 201
    function, body, _ = db.requests[-1]
    assert function == "save_meta_template_request_draft_v2"
    assert body["p_slack_channel"] == "D0123456789" and body["p_slack_thread_ts"] == "1762700000.123456"


def test_a_dm_without_thread_saves_only_the_channel() -> None:
    client, db = _client()
    thread = {"channel": "D0123456789", "thread_ts": None}
    assert client.post(f"{PREFIX}/template-requests", json=_body(slack_thread=thread), headers=AUTH).status_code == 201
    assert db.requests[-1][1]["p_slack_thread_ts"] is None


def test_without_a_thread_it_saves_as_before() -> None:
    client, db = _client()
    assert client.post(f"{PREFIX}/template-requests", json=_body(), headers=AUTH).status_code == 201
    assert db.requests[-1][0] == "save_meta_template_request_draft_v1"
    assert "p_slack_channel" not in db.requests[-1][1]


@pytest.mark.parametrize("thread", [
    {"channel": "no-es-canal", "thread_ts": None},
    {"channel": "D0123456789", "thread_ts": "ayer"},
    {"channel": "D0123456789"},
    {"channel": "D0123456789", "thread_ts": None, "extra": 1},
    "D0123456789",
])
def test_a_malformed_thread_is_refused(thread: object) -> None:
    client, db = _client()
    response = client.post(f"{PREFIX}/template-requests", json=_body(slack_thread=thread), headers=AUTH)
    assert response.status_code == 422
    assert not any("save_meta" in row[0] for row in db.requests)
