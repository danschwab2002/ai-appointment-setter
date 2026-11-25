"""Las rutas del masivo para el Automation Expert y su flag (encargo 6 del masivo manejado por el AE).

Piezas 7 y 11 del mapa del 2026-11-25 («El masivo manejado por el Automation Expert», en el vault). El AE
arma el borrador (``POST /internal/automation-expert/v1/bulk-sends``) y lee el estado; nunca confirma,
arranca ni frena. La regla del boton de baja se prueba con los catalogos capturados de la bandeja:

* ``chatwoot_inbox_11_message_templates_20261010.json``: ATT1, produccion, Chatwoot 4.13 (GET
  /inboxes/11). ``att1_carrito_abandonado_01`` tiene «No más mensajes»; ``att1_reactivacion_01`` no tiene
  botones y ``att1_seguimiento_descuento_01`` tiene solo un link que cambia por persona.
* ``chatwoot_message_templates_inbox_11_bienvenida_20261026.json``: ``att1_ficha_efectivo_01`` (un link fijo y
  «No más mensajes», UTILITY) y ``att1_bienvenida_01`` (encabezado de texto, sin la baja).
* ``chatwoot_message_templates_inbox_13_maribel_reconstruida_20261027.json``: la de Maribel, con «No mas
  mensajes» sin tilde (reconstruida del pedido a Meta, ver su ``_capture``).

La base se emula sobre PostgREST con la forma de ``create_bulk_send_v1``, ``read_bulk_send_v1`` y
``read_bulk_sends_v1`` de 20261125000200 (lo que la base hace de verdad lo prueba PGlite,
``validate_bulk_send_store.mjs``). La vista que devuelve trae ejemplos con telefono enmascarado e IDs de
Slack, como la real: la prueba es que no lleguen al agente.
"""

from __future__ import annotations

import copy
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from test_agent_product_routes import CHANGE
from test_flow_approval_page import ORIGIN, SERVICE_KEY, _NoSupabase, _settings

from bridge import app as app_module
from bridge import flow_change_api
from bridge.app import Settings, create_app
from bridge.flow_change_api import EntryRefused

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PREFIX = "/internal/automation-expert/v1/bulk-sends"
TOKEN = "p" * 48
ASKER = {"slack_user": "U0PIDEALAE1"}
INSTANCE = {"tenant_ref": "lancemos", "ally_ref": "att1"}
NOW = datetime(2026, 11, 25, 15, 0, tzinfo=UTC)
LATER = "2026-11-29T19:00:00-06:00"
# Telefonos de la lista falsa: ninguno puede aparecer en una respuesta, ni sus ultimos digitos con el prefijo.
PHONES = ("5215512345678", "573001234567", "5491155554444")


def _catalog(name: str) -> list[dict[str, Any]]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["message_templates"]


ATT1_CATALOG = _catalog("chatwoot_inbox_11_message_templates_20261010.json")
WELCOME_CATALOG = _catalog("chatwoot_message_templates_inbox_11_bienvenida_20261026.json")
MARIBEL_CATALOG = _catalog("chatwoot_message_templates_inbox_13_maribel_reconstruida_20261027.json")


def _with_image(template: dict[str, Any]) -> dict[str, Any]:
    """Un caso derivado (no capturado: ningun catalogo del repo tiene encabezado de imagen): la del carrito
    con un encabezado IMAGE, como lo publica Chatwoot."""
    derived = copy.deepcopy(template)
    derived["name"] = "att1_aviso_vivo_imagen_01"
    derived["components"].insert(0, {"type": "HEADER", "format": "IMAGE"})
    return derived


class Chatwoot:
    def __init__(self, templates: list[dict[str, Any]] | None = None, *, failure: Exception | None = None) -> None:
        self.templates = templates if templates is not None else ATT1_CATALOG
        self.failure = failure
        self.inboxes: list[int] = []

    async def get_inbox(self, *, inbox_id: int) -> dict[str, Any]:
        self.inboxes.append(inbox_id)
        if self.failure is not None:
            raise self.failure
        return {"id": inbox_id, "channel_type": "Channel::Whatsapp", "message_templates": copy.deepcopy(self.templates)}


class Db:
    """Las tres funciones del masivo que usa el AE, con la forma de 20261125000200."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.sends: dict[str, dict[str, Any]] = {}
        self.created: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {SERVICE_KEY}"
        name = request.url.path.rsplit("/", 1)[1]
        self.calls.append(name)
        body = json.loads(request.content)
        assert body["p_instance"] == INSTANCE
        return getattr(self, "_" + name)(body)

    def _create_bulk_send_v1(self, body: dict[str, Any]) -> httpx.Response:
        send = body["p_send"]
        self.created.append(send)
        send_id = str(uuid.uuid4())
        self.sends[send_id] = self.view(send_id, send)
        return httpx.Response(200, json={"ok": True, "send_id": send_id})

    def _read_bulk_send_v1(self, body: dict[str, Any]) -> httpx.Response:
        view = self.sends.get(body["p_send_id"])
        if view is None:
            # PostgREST 14 manda P0002 como error con su codigo (tests/fixtures/automation_expert_change/).
            return httpx.Response(400, json={"code": "P0002", "message": "bulk_send_not_found"})
        return httpx.Response(200, json=view)

    def _read_bulk_sends_v1(self, body: dict[str, Any]) -> httpx.Response:
        return httpx.Response(200, json=list(self.sends.values())[: body["p_limit"]])

    @staticmethod
    def view(send_id: str, send: dict[str, Any]) -> dict[str, Any]:
        """La forma de _bulk_send_view, con una lista ya subida y revisada."""
        return {
            "send_id": send_id, "state": "borrador", "template_name": send["template_name"],
            "template_language": send["template_language"], "template_variables": send["template_variables"],
            "header_image_url": send.get("header_image_url"), "campaign_label": send.get("campaign_label"),
            "product_refs": send.get("product_refs"), "scheduled_at": send["scheduled_at"],
            "window_mode": send.get("window_mode", "centrada"), "rate_millis": 2500, "recipient_cap": 6000,
            "created_by": "automation_expert", "created_at": "2026-11-25T15:00:00Z",
            "confirmed_by": "slack:U0DANSCHWAB1", "confirmed_at": None, "confirmed_count": None,
            "confirmed_health": None, "started_at": None, "finished_at": None, "brake_reason": None,
            "braked_by": None, "braked_at": None,
            "list": {"list_id": str(uuid.uuid4()), "state": "revisada", "uploaded_by": "slack:U0DANSCHWAB1",
                     "uploaded_at": "2026-11-25T15:10:00Z", "file_sha256": "a" * 64, "source_kind": "inscritas_ghl",
                     "source_note": f"la lista del vivo, la de {PHONES[0]}", "notice_declared": True,
                     "rejected_counts": {"sin_pais": 3, "duplicado": 2}, "member_count": 120,
                     "retention_days": 30, "vetted_at": "2026-11-25T15:11:00Z", "purged_at": None,
                     "eligible_count": 110, "excluded_by_reason": {"baja": 6, "compro": 4},
                     "examples": [{"phone": "…" + phone[-4:], "initial": "A."} for phone in PHONES]},
            "recipients_by_state": {},
        }


def _app(*, db: Db | None = None, chatwoot: Chatwoot | None = None,
         products: frozenset[str] = frozenset({"att1"})) -> tuple[TestClient, Db, Chatwoot]:
    """Las rutas sobre una app minima, como las registra create_app con el flag prendido."""
    db = db or Db()
    chatwoot = chatwoot or Chatwoot()
    store = flow_change_api.BulkSendStore(base_url="https://att1-rest.internal", db_key=SERVICE_KEY,
                                          transport=httpx.MockTransport(db.handler))
    app = FastAPI()

    @app.exception_handler(EntryRefused)
    async def entry_refused(_: Any, error: EntryRefused) -> JSONResponse:  # el de flow_change_api, en create_app
        return JSONResponse(status_code=422, content={"detail": error.code, "reason": error.reason})

    flow_change_api.register_bulk_send_routes(
        app, token=TOKEN, store=store, chatwoot=chatwoot, tenant_ref="lancemos", ally_ref="att1", inbox_id=11,
        product_refs=products, approval_origin=ORIGIN, git_sha="57e9792", clock=lambda: NOW)
    return TestClient(app, base_url=ORIGIN), db, chatwoot


def _ae(client: TestClient, method: str, path: str = "", body: dict | None = None, *,
        token: str = TOKEN) -> httpx.Response:
    return client.request(method, f"{PREFIX}{path}", json=body, headers={"Authorization": f"Bearer {token}"})


def _draft(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"template_name": "att1_carrito_abandonado_01",
                            "template_variables": [{"de": "nombre"}, "el Protocolo"],
                            "scheduled_at": LATER, "campaign_label": "vivo-29-11", "requested_by": ASKER}
    body.update(changes)
    return {key: value for key, value in body.items() if value is not None}


def _no_phone(response: httpx.Response) -> None:
    text = response.text
    for phone in PHONES:
        assert phone not in text and "…" + phone[-4:] not in text and "\\u2026" + phone[-4:] not in text, phone
    assert "examples" not in text and "U0DANSCHWAB1" not in text and "source_note" not in text


# ------------------------------------------------------------------ el borrador


def test_a_draft_with_the_opt_out_button_is_saved_and_brings_the_upload_link() -> None:
    client, db, chatwoot = _app()
    response = _ae(client, "POST", "", _draft())
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert chatwoot.inboxes == [11]
    assert db.calls == ["create_bulk_send_v1", "read_bulk_send_v1"]
    assert db.created == [{"template_name": "att1_carrito_abandonado_01", "template_language": "es_MX",
                           "template_variables": [{"de": "nombre"}, "el Protocolo"],
                           "scheduled_at": "2026-11-30T01:00:00Z", "window_mode": "centrada",
                           "product_refs": ["att1"], "created_by": "automation_expert",
                           "campaign_label": "vivo-29-11"}]
    send_id = data["send_id"]
    assert data["upload_url"] == f"{ORIGIN}/flow-approval/masivos/{send_id}"
    assert data["upload_url"] == flow_change_api.bulk_send_page_url(ORIGIN, send_id) == data["page_url"]
    assert "Entrar con Slack" in data["upload_note"] and "nunca sale desde este chat" in data["upload_note"]
    assert data["state"] == "borrador"
    assert data["list"]["eligible_count"] == 110 and data["list"]["excluded_by_reason"] == {"baja": 6, "compro": 4}
    _no_phone(response)


@pytest.mark.parametrize(("catalog", "name"), [
    (ATT1_CATALOG, "att1_carrito_abandonado_01"),
    (ATT1_CATALOG, "att1_compra_fallida_01"),
    (MARIBEL_CATALOG, "maribel_retomar_proceso_01"),  # «No mas mensajes», sin tilde: tocarla tambien es baja
])
def test_the_opt_out_rule_accepts_the_captured_templates_with_the_button(catalog: list, name: str) -> None:
    template = next(t for t in catalog if t["name"] == name)
    assert flow_change_api.template_has_opt_out_button(template)


@pytest.mark.parametrize(("catalog", "name"), [
    (ATT1_CATALOG, "att1_reactivacion_01"),          # sin botones
    (ATT1_CATALOG, "att1_seguimiento_descuento_01"),  # solo un link
    (WELCOME_CATALOG, "att1_bienvenida_01"),          # un link fijo, sin la baja
    (WELCOME_CATALOG, "att1_pago_efectivo_confirmado_01"),
])
def test_the_opt_out_rule_refuses_the_captured_templates_without_it(catalog: list, name: str) -> None:
    template = next(t for t in catalog if t["name"] == name)
    assert not flow_change_api.template_has_opt_out_button(template)


def test_a_url_button_or_another_quick_reply_with_that_text_does_not_count() -> None:
    template = copy.deepcopy(next(t for t in ATT1_CATALOG if t["name"] == "att1_carrito_abandonado_01"))
    buttons = next(c for c in template["components"] if c["type"] == "BUTTONS")["buttons"]
    buttons[2] = {"type": "URL", "text": "No más mensajes", "url": "https://example.test/baja"}
    assert not flow_change_api.template_has_opt_out_button(template)
    buttons[2] = {"type": "QUICK_REPLY", "text": "Ya no quiero"}
    assert not flow_change_api.template_has_opt_out_button(template)


def test_without_the_opt_out_button_the_draft_is_refused_and_nothing_is_saved() -> None:
    client, db, _ = _app(chatwoot=Chatwoot(WELCOME_CATALOG))
    response = _ae(client, "POST", "", _draft(template_name="att1_bienvenida_01", template_variables=None))
    assert response.status_code == 422
    assert response.json() == {"detail": "bulk_send_template_without_opt_out",
                               "reason": {"field": "template_name", "problem": "no_opt_out_quick_reply"}}
    assert db.calls == []


def test_a_template_not_approved_is_refused() -> None:
    catalog = copy.deepcopy(ATT1_CATALOG)
    next(t for t in catalog if t["name"] == "att1_carrito_abandonado_01")["status"] = "PENDING"
    client, db, _ = _app(chatwoot=Chatwoot(catalog))
    response = _ae(client, "POST", "", _draft())
    assert response.status_code == 422 and response.json()["detail"] == "bulk_send_template_not_approved"
    assert db.calls == []


@pytest.mark.parametrize(("changes", "code", "field"), [
    ({"template_name": "no_existe_01"}, "bulk_send_template_not_found", "template_name"),
    ({"template_language": "en_US"}, "bulk_send_template_not_found", "template_name"),
    ({"template_variables": [{"de": "nombre"}]}, "bulk_send_variables_mismatch", "template_variables"),
    ({"template_variables": ["a", "b", "c"]}, "bulk_send_variables_mismatch", "template_variables"),
    ({"template_variables": [{"de": "mail"}, "x"]}, "bulk_send_variables_invalid", "template_variables"),
    ({"template_variables": [{"de": "nombre"}, "llamá al 55 1234 5678"]}, "identifier_not_allowed",
     "template_variables"),
    ({"scheduled_at": "2026-11-25T15:30:00Z"}, "bulk_send_schedule_invalid", "scheduled_at"),
    ({"scheduled_at": "2027-01-30T15:30:00Z"}, "bulk_send_schedule_invalid", "scheduled_at"),
    ({"scheduled_at": "2026-11-29T19:00:00"}, "bulk_send_schedule_invalid", "scheduled_at"),
    ({"scheduled_at": "el sabado"}, "bulk_send_schedule_invalid", "scheduled_at"),
    ({"product_refs": ["otro"]}, "bulk_send_product_invalid", "product_refs"),
    ({"product_refs": ["att1", "att1"]}, "bulk_send_product_invalid", "product_refs"),
    ({"campaign_label": "Vivo 29"}, "bulk_send_invalid", "campaign_label"),
    ({"window_mode": "ya"}, "bulk_send_invalid", "window_mode"),
    ({"header_image_url": "https://img.example.test/vivo.jpg"}, "bulk_send_image_not_allowed", "header_image_url"),
    ({"header_image_url": "http://img.example.test/vivo.jpg"}, "bulk_send_image_invalid", "header_image_url"),
])
def test_a_draft_that_does_not_close_is_refused_with_its_field(changes: dict, code: str, field: str) -> None:
    client, db, _ = _app()
    response = _ae(client, "POST", "", _draft(**changes))
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == code and response.json()["reason"]["field"] == field
    assert db.calls == []
    assert "1234" not in response.text


def test_a_template_whose_link_changes_per_person_is_refused() -> None:
    # att1_seguimiento_descuento_01 no tiene la baja; con la baja sumada, igual el link {{1}} no se puede llenar.
    template = copy.deepcopy(next(t for t in ATT1_CATALOG if t["name"] == "att1_seguimiento_descuento_01"))
    next(c for c in template["components"] if c["type"] == "BUTTONS")["buttons"].append(
        {"type": "QUICK_REPLY", "text": "No más mensajes"})
    client, db, _ = _app(chatwoot=Chatwoot([template]))
    response = _ae(client, "POST", "", _draft(template_name="att1_seguimiento_descuento_01"))
    assert response.json()["detail"] == "bulk_send_template_per_person_parts" and db.calls == []


def test_an_image_header_needs_its_link_and_it_is_saved() -> None:
    template = _with_image(next(t for t in ATT1_CATALOG if t["name"] == "att1_carrito_abandonado_01"))
    client, db, _ = _app(chatwoot=Chatwoot([template]))
    response = _ae(client, "POST", "", _draft(template_name=template["name"]))
    assert response.json()["detail"] == "bulk_send_image_required" and db.calls == []
    response = _ae(client, "POST", "", _draft(template_name=template["name"],
                                              header_image_url="https://img.example.test/vivo.jpg"))
    assert response.status_code == 201
    assert db.created[0]["header_image_url"] == "https://img.example.test/vivo.jpg"


def test_with_several_products_the_draft_names_them() -> None:
    client, db, _ = _app(products=frozenset({"att1", "att1-mentoria"}))
    response = _ae(client, "POST", "", _draft())
    assert response.json()["detail"] == "bulk_send_product_required" and db.calls == []
    response = _ae(client, "POST", "", _draft(product_refs=["att1-mentoria"]))
    assert response.status_code == 201 and db.created[0]["product_refs"] == ["att1-mentoria"]


def test_without_the_catalog_nothing_is_saved() -> None:
    client, db, _ = _app(chatwoot=Chatwoot(failure=RuntimeError("caido")))
    response = _ae(client, "POST", "", _draft())
    assert response.status_code == 503 and response.json()["detail"] == "template_catalog_unavailable"
    assert db.calls == []


@pytest.mark.parametrize("changes", [
    {"requested_by": {"slack_user": "dan"}},
    {"requested_by": None},
    {"phones": ["5215512345678"]},
    {"list": "data:text/csv;base64,AAAA"},
])
def test_the_body_takes_no_list_and_needs_who_asks(changes: dict) -> None:
    client, db, _ = _app()
    response = _ae(client, "POST", "", _draft(**changes))
    assert response.status_code == 422 and response.json()["detail"] == "invalid_body"
    assert db.calls == [] and "5215512345678" not in response.text


def test_a_rejection_of_the_base_comes_back_with_its_code() -> None:
    db = Db()
    db._create_bulk_send_v1 = lambda body: httpx.Response(  # type: ignore[method-assign]
        400, json={"code": "22023", "message": "bulk_send_invalid"})
    client, _, _ = _app(db=db)
    response = _ae(client, "POST", "", _draft())
    assert response.status_code == 422 and response.json()["detail"] == "bulk_send_invalid"


def test_without_the_token_nothing_is_read() -> None:
    client, db, chatwoot = _app()
    for method, path, body in (("POST", "", _draft()), ("GET", "", None), ("GET", f"/{uuid.uuid4()}", None)):
        assert _ae(client, method, path, body, token="x" * 48).status_code == 401
    assert db.calls == [] and chatwoot.inboxes == []


# ------------------------------------------------------------------ las lecturas


def test_get_and_list_bring_the_counts_and_never_a_phone() -> None:
    client, db, _ = _app()
    send_id = _ae(client, "POST", "", _draft()).json()["data"]["send_id"]
    one = _ae(client, "GET", f"/{send_id}")
    assert one.status_code == 200
    data = one.json()["data"]
    assert data["send_id"] == send_id and data["page_url"] == f"{ORIGIN}/flow-approval/masivos/{send_id}"
    assert data["list"] == {"state": "revisada", "uploaded_at": "2026-11-25T15:10:00Z",
                            "source_kind": "inscritas_ghl", "notice_declared": True,
                            "rejected_counts": {"sin_pais": 3, "duplicado": 2}, "member_count": 120,
                            "retention_days": 30, "vetted_at": "2026-11-25T15:11:00Z", "purged_at": None,
                            "eligible_count": 110, "excluded_by_reason": {"baja": 6, "compro": 4}}
    assert "created_by" not in data and "confirmed_by" not in data and "braked_by" not in data
    _no_phone(one)
    listed = _ae(client, "GET", "?limit=5")
    assert listed.status_code == 200 and [s["send_id"] for s in listed.json()["data"]["bulk_sends"]] == [send_id]
    _no_phone(listed)
    assert db.calls[-2:] == ["read_bulk_send_v1", "read_bulk_sends_v1"]


def test_reads_refuse_what_does_not_close() -> None:
    client, db, _ = _app()
    assert _ae(client, "GET", "/no-es-uuid").json()["detail"] == "invalid_bulk_send_id"
    missing = _ae(client, "GET", f"/{uuid.uuid4()}")
    assert missing.status_code == 404 and missing.json()["detail"] == "bulk_send_not_found"
    for limit in ("0", "51", "x"):
        assert _ae(client, "GET", f"?limit={limit}").json()["detail"] == "invalid_query"


def test_no_route_confirms_starts_or_brakes() -> None:
    client, _, _ = _app()
    paths = {(tuple(sorted(route.methods)), route.path) for route in client.app.routes
             if getattr(route, "path", "").startswith(PREFIX)}
    assert paths == {(("POST",), PREFIX), (("GET",), PREFIX), (("GET",), PREFIX + "/{send_id}")}
    for suffix in ("confirm", "start", "brake"):
        assert _ae(client, "POST", f"/{uuid.uuid4()}/{suffix}", {}).status_code in (404, 405)


# ------------------------------------------------------------------ el flag en create_app


def _create_app(*, enabled: bool) -> tuple[TestClient, Db]:
    db = Db()
    store = flow_change_api.BulkSendStore(base_url="https://att1-rest.internal", db_key=SERVICE_KEY,
                                          transport=httpx.MockTransport(db.handler))
    settings = _settings(flow_approval_enabled=True, **CHANGE, bulk_send_enabled=enabled)
    app = create_app(settings, supabase_client=_NoSupabase(),  # type: ignore[arg-type]
                     chatwoot_client=Chatwoot(), bulk_send_store=store)  # type: ignore[arg-type]
    return TestClient(app, base_url=ORIGIN), db


def test_off_by_default_and_portable() -> None:
    assert Settings.__dataclass_fields__["bulk_send_enabled"].default is False
    assert "bulk_send_enabled" in app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES


def test_with_the_flag_off_the_routes_do_not_exist_and_the_base_hears_nothing() -> None:
    client, db = _create_app(enabled=False)
    for method, path, body in (("POST", "", _draft()), ("GET", "", None), ("GET", f"/{uuid.uuid4()}", None)):
        response = _ae(client, method, path, body)
        assert response.status_code in (404, 405) and response.json()["detail"] in ("Not Found", "Method Not Allowed")
    assert db.calls == []
    assert not [route for route in client.app.routes if "/bulk-sends" in getattr(route, "path", "")]


def test_with_the_flag_on_create_app_registers_the_routes() -> None:
    client, db = _create_app(enabled=True)
    listed = _ae(client, "GET", "")
    assert listed.status_code == 200 and listed.json()["data"] == {"bulk_sends": []}
    assert db.calls == ["read_bulk_sends_v1"]


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
    monkeypatch.delenv(flow_change_api.BULK_SEND_ENV, raising=False)
    assert Settings.from_env().bulk_send_enabled is False
    monkeypatch.setenv(flow_change_api.BULK_SEND_ENV, "true")
    assert Settings.from_env().bulk_send_enabled is True


@pytest.mark.parametrize(("overrides", "message"), [
    ({"automation_expert_change_enabled": False}, "requires AUTOMATION_EXPERT_CHANGE_ENABLED"),
    ({"flow_approval_enabled": False}, "requires FLOW_APPROVAL_ENABLED"),
    ({"instance_manifest": None}, "requires an instance manifest"),
    ({"supabase_service_role_key": None}, "requires SUPABASE_BASE_URL and SUPABASE_SERVICE_ROLE_KEY"),
])
def test_the_flag_needs_the_change_routes_the_page_and_the_base(overrides: dict, message: str) -> None:
    settings = _settings(**{**CHANGE, **overrides}, bulk_send_enabled=True)
    with pytest.raises(ValueError, match=message):
        flow_change_api.check_bulk_send_settings(settings)


def test_the_upload_link_waits_for_the_page_of_assignment_5() -> None:
    # La pagina la sirve el encargo 5; hasta entonces el link es solo una constante con el patron de la pagina.
    assert flow_change_api.BULK_SEND_PAGE_PATH == "/flow-approval/masivos"
    assert flow_change_api.bulk_send_page_url(ORIGIN + "/", str(uuid.UUID(int=1))) == (
        f"{ORIGIN}/flow-approval/masivos/00000000-0000-0000-0000-000000000001")
    assert NOW + timedelta(hours=1) == datetime(2026, 11, 25, 16, 0, tzinfo=UTC)
