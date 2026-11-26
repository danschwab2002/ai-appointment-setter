"""Exportar contactos (contact-export-v1, funcion 1.3; pieza 4 del mapa de Z1 del 2026-11-26).

Las entradas:

* La forma de una pagina de personas es ``tests/fixtures/contact_export/people_page_shape_20261126.json``: las
  claves medidas en solo lectura contra la base de ATT1 y la de Maribel el 2026-11-26 (ver su ``_capture``), con
  filas sinteticas. Lo que la base hace de verdad lo prueba PGlite (``validate_contact_export.mjs``).
* Las etiquetas salen de ``ChatwootLabelIndex`` sobre las conversaciones capturadas de ATT1 del 2026-10-15
  (``chatwoot_daily_feedback_att1_conversations_20261015.json``, inbox 11, telefonos ya sinteticos). Las dos
  vienen sin etiquetas: el caso con etiquetas es esa misma conversacion con ``labels`` puestas (derivado).
* La pagina usa los dobles de ``test_flow_approval_page`` (la sesion de «Entrar con Slack» y PostgREST) con las
  funciones ``approver_*_contact_export_v1`` y sus reglas copiadas de la migracion.

Lo que se prueba sobre todo: que ningun telefono, mail ni nombre vuelva al AE (D7), que el CSV salga una sola vez
y antes de vencer (D12), que las bajas queden afuera por defecto y que el flag apagado no cambie nada (D3).
"""

from __future__ import annotations

import asyncio
import copy
import csv
import io
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient
from test_agent_product_routes import CHANGE
from test_flow_approval_page import (
    CLIENT_ID,
    CLIENT_SECRET,
    DAN,
    DB_KEY,
    ORIGIN,
    OUTSIDER,
    SERVICE_KEY,
    PostgrestEmulator,
    SlackEmulator,
    _Clock,
    _NoSupabase,
    _settings,
    _sign_in,
)

from bridge import app as app_module
from bridge import contact_export as ce
from bridge import flow_approval
from bridge.app import Settings, create_app
from bridge.flow_audience_store import AudienceLabelsUnavailable, ChatwootLabelIndex
from bridge.flow_change_api import EntryRefused

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SHAPE = json.loads((FIXTURES / "contact_export" / "people_page_shape_20261126.json").read_text(encoding="utf-8"))
CONVERSATIONS = json.loads(
    (FIXTURES / "chatwoot_daily_feedback_att1_conversations_20261015.json").read_text(encoding="utf-8"))
AE_PATH = "/internal/automation-expert/v1/contact-exports"
PAGE = "/flow-approval/exportar"
TOKEN = CHANGE["automation_expert_change_token"]
ASKER = {"slack_user": "U0PIDEALAE1"}
INSTANCE = {"tenant_ref": "lancemos", "ally_ref": "att1"}
CDMX = ZoneInfo("America/Mexico_City")
FORM = {"Content-Type": "application/x-www-form-urlencoded", "Origin": ORIGIN}


def person(phone: str, *, name: str | None = "Persona Prueba", email: str | None = "persona@example.test",
           forms: list[tuple[str, str]] | None = None, bought: list[str] | None = None,
           opted_out: bool = False) -> dict[str, Any]:
    """Una fila con la forma de la fixture capturada."""
    row = {"phone": phone, "name": name, "email": email,
           "forms": [{"at": at, "product": product} for at, product in (forms or [])],
           "purchased_products": list(bought or []), "opted_out": opted_out}
    assert set(row) == set(SHAPE["page"]["people"][0])
    return row


# Tres personas sinteticas: una de Mexico que compro, una de Argentina dada de baja y una de Colombia con un
# formulario y un nombre que una planilla leeria como formula.
MX, AR, CO = "525550000001", "541155550002", "573000000003"
PEOPLE = [
    person(MX, name="Ana Prueba", email="ana.prueba@example.test",
           forms=[("2026-11-20T15:04:05+00:00", "att1")], bought=["att1"]),
    person(AR, name="Bea Prueba", email="bea@example.test", opted_out=True,
           forms=[("2026-11-21T10:00:00+00:00", "att1")]),
    person(CO, name='=HYPERLINK("http://x.test","clic")', email="@raro@example.test",
           forms=[("2026-11-10T04:00:00+00:00", "att1")]),
]
PERSONAL = ("5550000001", "155550002", "3000000003", "Ana Prueba", "Bea Prueba", "ana.prueba@example.test",
            "bea@example.test", "HYPERLINK", "raro@example.test")


def _paged(people: list[dict[str, Any]], after: str | None, limit: int) -> dict[str, Any]:
    rows = sorted((p for p in people if after is None or p["phone"] > after), key=lambda p: p["phone"])[:limit]
    return {"people": rows, "next_after": rows[-1]["phone"] if len(rows) == limit else None}


def _conversation(labels: list[str], phone: str) -> dict[str, Any]:
    conversation = copy.deepcopy(CONVERSATIONS["conversations"]["120"]["conversation"])
    conversation["labels"] = labels
    conversation["meta"]["sender"]["phone_number"] = "+" + phone
    return {"conversation": conversation}


class FakeChatwoot:
    """El listado del cliente de control (``list_recent_conversations_with_messages``) y el catalogo."""

    def __init__(self, items: list[dict[str, Any]] | None = None, *, failure: Exception | None = None) -> None:
        self.items = items if items is not None else [
            _conversation(["black-2026"], "5215550000001"),  # la forma de WhatsApp del de Mexico
            _conversation(["vip"], CO),
        ]
        self.failure = failure
        self.asked: list[dict[str, Any]] = []

    async def list_recent_conversations_with_messages(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.asked.append(kwargs)
        if self.failure is not None:
            raise self.failure
        return copy.deepcopy(self.items)

    async def get_inbox(self, *, inbox_id: int) -> dict[str, Any]:
        return {"id": inbox_id, "message_templates": []}


def _labels(chatwoot: FakeChatwoot | None = None) -> ChatwootLabelIndex:
    return ChatwootLabelIndex(chatwoot=chatwoot or FakeChatwoot(), inbox_id=11,
                              clock=lambda: datetime(2026, 11, 26, 15, 0, tzinfo=UTC))


def _collect(filters: dict[str, Any] | None = None, *, people: list[dict[str, Any]] | None = None,
             columns: tuple[str, ...] = ce.DEFAULT_COLUMNS, labels: Any = "default") -> ce.Collected:
    rows = PEOPLE if people is None else people

    async def read(after: str | None, limit: int) -> dict[str, Any]:
        return _paged(rows, after, limit)

    parsed = ce.ExportFilters.parse(filters, products=frozenset({"att1"}))
    return asyncio.run(ce.collect(read, labels=_labels() if labels == "default" else labels, filters=parsed,
                                  columns=columns, zone=CDMX))


# ----------------------------------------------------------------- la forma, el telefono y el CSV


def test_the_captured_shape_parses() -> None:
    people, after = ce.parse_people(SHAPE["page"])
    assert after == "541155550002"
    assert [p.phone for p in people] == ["525550000001", "541155550002"]
    assert people[0].last_form_at == datetime(2026, 11, 20, 15, 4, 5, 123456, tzinfo=UTC)
    assert people[0].purchased_products == {"att1"} and people[1].opted_out is True
    assert people[1].name is None and people[1].last_form_at is None


@pytest.mark.parametrize("broken", [
    {"people": [{**SHAPE["page"]["people"][0], "phone": "+525550000001"}], "next_after": None},
    {"people": [{**SHAPE["page"]["people"][0], "opted_out": "no"}], "next_after": None},
    {"people": [{**SHAPE["page"]["people"][0], "forms": [{"at": "ayer", "product": "att1"}]}], "next_after": None},
    {"people": [], "next_after": "no-es-telefono"},
    {"people": "todas"},
])
def test_a_row_out_of_shape_stops_everything(broken: dict[str, Any]) -> None:
    with pytest.raises(ce.ExportStoreUnavailable):
        ce.parse_people(broken)


def test_the_phone_goes_out_in_e164_and_the_country_by_its_code() -> None:
    assert ce.export_e164("525550000001") == "+525550000001"  # Mexico, sin el 1
    assert ce.export_e164("5215550000001") == "+525550000001"
    assert ce.export_e164("541155550002") == "+5491155550002"  # Argentina, el movil
    assert ce.export_e164("573000000003") == "+573000000003"
    assert (ce.country_of(MX), ce.country_of(AR), ce.country_of(CO)) == ("MX", "AR", "CO")


def test_the_csv_neutralizes_formulas_and_keeps_the_default_columns() -> None:
    found = _collect(people=PEOPLE)
    text = ce.export_csv(found.people, ce.DEFAULT_COLUMNS, zone=CDMX)
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == ["nombre", "telefono", "email", "etiquetas", "compro", "ultimo_formulario"]
    by_phone = {row[1]: row for row in rows[1:]}
    # Sin la baja (D12): dos filas. El telefono lleva + adelante, asi que lleva el apostrofo.
    assert set(by_phone) == {"'+525550000001", "'+573000000003"}
    assert by_phone["'+525550000001"] == ["Ana Prueba", "'+525550000001", "ana.prueba@example.test", "black-2026",
                                          "si", "2026-11-20T09:04:05-06:00"]
    colombia = by_phone["'+573000000003"]
    assert colombia[0] == "'=HYPERLINK(\"http://x.test\",\"clic\")" and colombia[2] == "'@raro@example.test"
    assert colombia[3:5] == ["vip", "no"]
    assert not any(cell[:1] in "=+-@" for row in rows for cell in row if cell)


def test_the_optional_columns() -> None:
    found = _collect({"incluir_bajas": True})
    text = ce.export_csv(found.people, ("telefono", "pais", "baja", "productos"), zone=CDMX)
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[1:] == [["'+525550000001", "MX", "no", "att1"], ["'+5491155550002", "AR", "si", ""],
                        ["'+573000000003", "CO", "no", ""]]


# ----------------------------------------------------------------- los filtros


def test_without_filters_everyone_but_the_opt_outs_and_they_are_counted() -> None:
    found = _collect()
    assert [p.phone for p in found.people] == [MX, CO]
    assert (found.total, found.opted_out_excluded) == (3, 1)


@pytest.mark.parametrize(("filters", "expected"), [
    ({"etiquetas": ["black-2026"]}, [MX]),
    ({"etiquetas": ["Black-2026", "vip"]}, [MX, CO]),
    ({"sin_etiquetas": ["vip"]}, [MX]),
    ({"compro": True}, [MX]),
    ({"compro": False}, [CO]),
    ({"compro": False, "incluir_bajas": True}, [AR, CO]),
    ({"compro": True, "producto": "att1"}, [MX]),
    # 2026-11-10T04:00Z es el 9 de noviembre en CDMX: la ventana va en la zona de la instancia.
    ({"formulario_desde": "2026-11-10"}, [MX]),
    ({"formulario_hasta": "2026-11-09"}, [CO]),
    ({"formulario_desde": "2026-11-09", "formulario_hasta": "2026-11-20", "producto": "att1"}, [MX, CO]),
    ({"paises": ["co"]}, [CO]),
    ({"paises": ["AR"]}, []),
    ({"paises": ["AR"], "incluir_bajas": True}, [AR]),
])
def test_each_filter(filters: dict[str, Any], expected: list[str]) -> None:
    assert [p.phone for p in _collect(filters).people] == expected


@pytest.mark.parametrize(("filters", "code", "field"), [
    ({"telefonos": ["5215550000001"]}, "contact_export_filter_invalid", "filtros"),
    ({"5215550000001": True}, "contact_export_filter_invalid", "filtros"),
    ({"compro": "si"}, "contact_export_filter_invalid", "compro"),
    ({"etiquetas": []}, "contact_export_filter_invalid", "etiquetas"),
    ({"etiquetas": ["con espacio"]}, "contact_export_filter_invalid", "etiquetas"),
    ({"etiquetas": ["a"], "sin_etiquetas": ["a"]}, "contact_export_filter_invalid", "sin_etiquetas"),
    ({"producto": "att1"}, "contact_export_filter_invalid", "producto"),
    ({"producto": "otro", "compro": True}, "contact_export_product_invalid", "producto"),
    ({"formulario_desde": "2026-11-31"}, "contact_export_filter_invalid", "formulario_desde"),
    ({"formulario_desde": "2026-11-20", "formulario_hasta": "2026-11-01"}, "contact_export_filter_invalid",
     "formulario_hasta"),
    ({"paises": ["MEX"]}, "contact_export_filter_invalid", "paises"),
    ({"etiquetas_dias": 400}, "contact_export_filter_invalid", "etiquetas_dias"),
    ({"incluir_bajas": "si"}, "contact_export_filter_invalid", "incluir_bajas"),
])
def test_a_filter_that_does_not_close_says_which_field(filters: dict[str, Any], code: str, field: str) -> None:
    with pytest.raises(EntryRefused) as refused:
        ce.ExportFilters.parse(filters, products=frozenset({"att1"}))
    assert refused.value.code == code and refused.value.reason["field"] == field
    # El motivo nunca trae el valor (puede ser un telefono).
    assert "5215550000001" not in json.dumps(refused.value.reason)


def test_the_columns() -> None:
    assert ce.parse_columns(None) == ce.DEFAULT_COLUMNS
    assert ce.parse_columns(["telefono", "pais"]) == ("telefono", "pais")
    for bad in ([], ["telefono", "telefono"], ["direccion"], "telefono"):
        with pytest.raises(EntryRefused):
            ce.parse_columns(bad)


def test_the_filters_round_trip_through_what_is_stored() -> None:
    raw = {"etiquetas": ["vip"], "compro": False, "producto": "att1", "formulario_desde": "2026-11-01",
           "paises": ["MX"], "etiquetas_dias": 30}
    parsed = ce.ExportFilters.parse(raw, products=frozenset({"att1"}))
    assert ce.ExportFilters.parse(parsed.to_json(), products=frozenset({"att1"})) == parsed
    assert ce.ExportFilters().to_json() == {}


# ----------------------------------------------------------------- la base, las etiquetas y el tope


def test_the_base_is_read_by_pages_until_the_end() -> None:
    many = [person(f"5255500{i:05d}") for i in range(2500)]
    asked: list[str | None] = []

    async def read(after: str | None, limit: int) -> dict[str, Any]:
        asked.append(after)
        assert limit == ce.PEOPLE_PAGE
        return _paged(many, after, limit)

    found = asyncio.run(ce.collect(read, labels=None, filters=ce.ExportFilters(), columns=("telefono",), zone=CDMX))
    assert found.total == 2500 and len(found.people) == 2500
    assert asked == [None, "525550000999", "525550001999"]


def test_more_people_than_the_cap_is_refused_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ce, "MAX_PEOPLE", 3)
    monkeypatch.setattr(ce, "PEOPLE_PAGE", 2)
    with pytest.raises(ce.ExportTooLarge):
        _collect(people=[person(f"52555000000{i}") for i in range(4)], columns=("telefono",), labels=None)


def test_chatwoot_is_asked_only_when_labels_are_needed() -> None:
    chatwoot = FakeChatwoot(failure=AssertionError("no se pide"))
    found = _collect(columns=("nombre", "telefono", "compro"), labels=_labels(chatwoot))
    assert len(found.people) == 2 and chatwoot.asked == []
    chatwoot = FakeChatwoot()
    _collect({"etiquetas_dias": 30}, columns=("telefono", "etiquetas"), labels=_labels(chatwoot))
    assert chatwoot.asked[0]["include_messages"] is False and chatwoot.asked[0]["expected_inbox_id"] == 11
    assert chatwoot.asked[0]["active_since_epoch"] == int(datetime(2026, 10, 27, 15, 0, tzinfo=UTC).timestamp())


def test_labels_that_cannot_be_read_whole_stop_the_export() -> None:
    for labels in (_labels(FakeChatwoot(failure=RuntimeError("conversation_scan_incomplete"))),
                   _labels(FakeChatwoot([_conversation(["vip"], CO) | {}] + [
                       {"conversation": {**_conversation(["vip"], CO)["conversation"],
                                         "meta": {"sender": {"phone_number": None}}}}])),
                   None):
        with pytest.raises(AudienceLabelsUnavailable):
            _collect({"etiquetas": ["vip"]}, labels=labels)


# ----------------------------------------------------------------- las rutas del AE


class ServiceDb:
    """Las tres funciones del bridge de 20261126300000, con la forma y las reglas de la migracion."""

    def __init__(self, people: list[dict[str, Any]] | None = None) -> None:
        self.people = PEOPLE if people is None else people
        self.calls: list[str] = []
        self.created: list[dict[str, Any]] = []
        self.exports: dict[str, dict[str, Any]] = {}
        self.rate_limited = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {SERVICE_KEY}"
        name = request.url.path.rsplit("/", 1)[1]
        self.calls.append(name)
        body = json.loads(request.content)
        assert body["p_instance"] == INSTANCE
        if name == "read_contact_export_people_v1":
            return httpx.Response(200, json=_paged(self.people, body["p_after"], body["p_limit"]))
        if name == "create_contact_export_v1":
            if self.rate_limited:
                return httpx.Response(200, json={"ok": False, "reason_code": "contact_export_rate_limited"})
            export = body["p_export"]
            assert set(export) == {"filters", "columns", "matched_count", "requested_by"}
            self.created.append(export)
            export_id = str(uuid.uuid4())
            self.exports[export_id] = {
                "export_id": export_id, "state": "pendiente", "filters": export["filters"],
                "columns": export["columns"], "matched_count": export["matched_count"],
                "created_at": "2026-11-26T15:00:00+00:00", "expires_at": "2026-11-26T15:30:00+00:00",
                "downloaded_at": None, "downloaded_count": None}
            return httpx.Response(200, json={"ok": True, "export_id": export_id,
                                             "expires_at": "2026-11-26T15:30:00+00:00"})
        if name == "read_contact_export_v1":
            view = self.exports.get(body["p_export_id"])
            if view is None:
                return httpx.Response(400, json={"code": "P0002", "message": "contact_export_not_found"})
            return httpx.Response(200, json=view)
        raise AssertionError(name)


def _create_app(*, enabled: bool, db: ServiceDb | None = None, chatwoot: FakeChatwoot | None = None,
                supabase: Any = None) -> tuple[TestClient, ServiceDb]:
    db = db or ServiceDb()
    store = ce.ContactExportStore(base_url="https://att1-rest.internal", db_key=SERVICE_KEY,
                                  transport=httpx.MockTransport(db.handler))
    settings = _settings(flow_approval_enabled=True, **CHANGE, contact_export_enabled=enabled)
    app = create_app(settings, supabase_client=supabase or _NoSupabase(),  # type: ignore[arg-type]
                     chatwoot_client=chatwoot or FakeChatwoot(),  # type: ignore[arg-type]
                     contact_export_store=store)
    return TestClient(app, base_url=ORIGIN), db


def _ae(client: TestClient, method: str, path: str = "", body: dict[str, Any] | None = None) -> httpx.Response:
    return client.request(method, AE_PATH + path, headers={"Authorization": f"Bearer {TOKEN}"},
                          content=json.dumps(body) if body is not None else None)


def test_off_by_default_and_portable() -> None:
    assert Settings.__dataclass_fields__["contact_export_enabled"].default is False
    assert "contact_export_enabled" in app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES


def test_with_the_flag_off_no_route_exists_and_the_base_hears_nothing() -> None:
    client, db = _create_app(enabled=False)
    for method, path, body in (("POST", "", {"requested_by": ASKER}), ("GET", f"/{uuid.uuid4()}", None)):
        response = _ae(client, method, path, body)
        assert response.status_code in (404, 405)
    assert client.get(f"{PAGE}/{uuid.uuid4()}").status_code == 404
    assert client.post(f"{PAGE}/{uuid.uuid4()}/csv", headers=FORM, content="csrf_token=x").status_code in (404, 405)
    assert db.calls == []
    assert not [route for route in client.app.routes
                if "contact-exports" in getattr(route, "path", "") or "/exportar" in getattr(route, "path", "")]


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
    monkeypatch.delenv(ce.CONTACT_EXPORT_ENV, raising=False)
    assert Settings.from_env().contact_export_enabled is False
    monkeypatch.setenv(ce.CONTACT_EXPORT_ENV, "true")
    assert Settings.from_env().contact_export_enabled is True


@pytest.mark.parametrize(("overrides", "message"), [
    ({"automation_expert_change_enabled": False}, "requires AUTOMATION_EXPERT_CHANGE_ENABLED"),
    ({"flow_approval_enabled": False}, "requires FLOW_APPROVAL_ENABLED"),
    ({"instance_manifest": None}, "requires an instance manifest"),
    ({"supabase_service_role_key": None}, "requires SUPABASE_BASE_URL and SUPABASE_SERVICE_ROLE_KEY"),
])
def test_the_flag_needs_the_change_routes_the_page_and_the_base(overrides: dict, message: str) -> None:
    settings = _settings(**{**CHANGE, **overrides}, contact_export_enabled=True)
    with pytest.raises(ValueError, match=message):
        ce.check_settings(settings)


def test_the_ae_gets_the_count_and_the_link_and_nothing_personal() -> None:
    client, db = _create_app(enabled=True)
    response = _ae(client, "POST", "", {"filtros": {"compro": False}, "requested_by": ASKER})
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["matched_count"] == 1 and data["people_in_base"] == 3 and data["opted_out_excluded"] == 1
    assert data["page_url"] == f"{ORIGIN}{PAGE}/{data['export_id']}" and data["expires_in_minutes"] == 30
    assert data["columns"] == list(ce.DEFAULT_COLUMNS)
    for text in PERSONAL:
        assert text not in response.text
    assert db.created == [{"filters": {"compro": False}, "columns": list(ce.DEFAULT_COLUMNS),
                           "matched_count": 1, "requested_by": "U0PIDEALAE1"}]
    labelled = _ae(client, "POST", "", {"filtros": {"etiquetas": ["vip", "black-2026"]}, "requested_by": ASKER})
    assert labelled.json()["data"]["matched_count"] == 2
    assert db.created[-1]["filters"] == {"etiquetas": ["black-2026", "vip"]}
    read = _ae(client, "GET", f"/{data['export_id']}")
    assert read.status_code == 200 and read.json()["data"]["state"] == "pendiente"
    assert read.json()["data"]["page_url"] == data["page_url"]


def test_ready_says_enabled_only_with_the_flag() -> None:
    from test_flow_change_api_app import SharedSupabaseSpy
    off, _ = _create_app(enabled=False, supabase=SharedSupabaseSpy())
    on, _ = _create_app(enabled=True, supabase=SharedSupabaseSpy())
    assert off.get("/ready").status_code == 200
    assert "contact_export" not in json.dumps(off.get("/ready").json())
    assert '"contact_export": "enabled"' in json.dumps(on.get("/ready").json())


@pytest.mark.parametrize(("body", "status", "code"), [
    ({"requested_by": {"slack_user": "nadie"}}, 422, "invalid_body"),
    ({"filtros": {"telefono": "5215550000001"}, "requested_by": ASKER}, 422, "contact_export_filter_invalid"),
    ({"columnas": ["direccion"], "requested_by": ASKER}, 422, "contact_export_columns_invalid"),
    ({"telefonos": ["5215550000001"], "requested_by": ASKER}, 422, "invalid_body"),
])
def test_a_wrong_request_never_reaches_the_base(body: dict[str, Any], status: int, code: str) -> None:
    client, db = _create_app(enabled=True)
    response = _ae(client, "POST", "", body)
    assert response.status_code == status and response.json()["detail"] == code
    assert "5215550000001" not in response.text
    assert db.calls == []


def test_without_the_labels_whole_nothing_is_created() -> None:
    client, db = _create_app(enabled=True, chatwoot=FakeChatwoot(failure=RuntimeError("conversation_scan_incomplete")))
    response = _ae(client, "POST", "", {"requested_by": ASKER})
    assert response.status_code == 503 and response.json()["detail"] == "contact_export_labels_unavailable"
    assert "create_contact_export_v1" not in db.calls


def test_the_rate_limit_and_the_auth() -> None:
    db = ServiceDb()
    db.rate_limited = True
    client, _ = _create_app(enabled=True, db=db)
    response = _ae(client, "POST", "", {"filtros": {"compro": True}, "requested_by": ASKER})
    assert response.status_code == 429 and response.json()["detail"] == "contact_export_rate_limited"
    unauthorized = client.post(AE_PATH, content=json.dumps({"requested_by": ASKER}))
    assert unauthorized.status_code == 401
    assert _ae(client, "GET", "/no-es-uuid").json()["detail"] == "invalid_contact_export_id"
    assert _ae(client, "GET", f"/{uuid.uuid4()}").json()["detail"] == "contact_export_not_found"


# ----------------------------------------------------------------- la pagina


class ExportDb(PostgrestEmulator):
    """Las funciones approver_*_contact_export_v1 con las reglas de 20261126300000."""

    def __init__(self) -> None:
        super().__init__()
        self.export_id = "0000000f-0000-4000-8000-0000000e0001"
        self.export = {
            "export_id": self.export_id, "state": "pendiente", "filters": {"compro": False},
            "columns": list(ce.DEFAULT_COLUMNS), "matched_count": 1, "created_at": "2026-11-26T15:00:00+00:00",
            "expires_at": "2026-11-26T15:30:00+00:00", "downloaded_at": None, "downloaded_count": None,
            "requested_by": "slack:U0PIDEALAE1", "downloaded_by": None}
        self.export_calls: list[tuple[str, dict]] = []
        self.race = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[1]
        if "contact_export" not in name:
            return super().handler(request)
        body = json.loads(request.content)
        self.export_calls.append((name, body))
        if self._session(body) is None:
            return self._raise("28000", "flow_approval_session_invalid", 403)
        if body["p_export_id"] != self.export_id:
            return self._raise("P0002", "contact_export_not_found", 404)
        state = self.export["state"]
        if name == "approver_read_contact_export_v1":
            return httpx.Response(200, json=self.export)
        if name == "approver_read_contact_export_people_v1":
            if state != "pendiente":
                code = "contact_export_already_downloaded" if state == "bajado" else "contact_export_expired"
                return httpx.Response(200, json={"ok": False, "reason_code": code})
            return httpx.Response(200, json={"ok": True, **_paged(PEOPLE, body["p_after"], body["p_limit"])})
        if name == "approver_mark_contact_export_downloaded_v1":
            if self.race:  # otra persona lo bajo entre que se leyo y se marco
                state = self.export["state"] = "bajado"
            if state != "pendiente":
                code = "contact_export_already_downloaded" if state == "bajado" else "contact_export_expired"
                return httpx.Response(200, json={"ok": False, "reason_code": code})
            self.export.update(state="bajado", downloaded_at="2026-11-26T15:10:00+00:00",
                               downloaded_count=body["p_count"])
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(name)


class Harness:
    def __init__(self, *, chatwoot: FakeChatwoot | None = None) -> None:
        self.db, self.slack, self.clock = ExportDb(), SlackEmulator(), _Clock()
        config = flow_approval.check_settings(_settings())
        assert config is not None
        from fastapi import FastAPI
        self.app = FastAPI()
        flow_approval.register_flow_approval_routes(
            self.app, config=config,
            store=flow_approval.ApprovalStore(base_url="https://att1-rest.internal", db_key=DB_KEY,
                                              transport=httpx.MockTransport(self.db.handler)),
            oidc=flow_approval.SlackIdTokenClient(client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                                                  transport=httpx.MockTransport(self.slack.handler),
                                                  clock=lambda: self.clock.now),
            time_zone="America/Mexico_City",
            extra_routes=ce.page_routes(labels=_labels(chatwoot),
                                        clock=lambda: datetime(2026, 11, 26, 15, 10, tzinfo=UTC)))
        self.client = TestClient(self.app, base_url=ORIGIN)

    def sign_in(self, user: str = DAN) -> Harness:
        _sign_in(self.client, self.slack, self.clock, user=user)
        return self

    def csrf(self) -> str:
        response = self.client.get(f"{PAGE}/{self.db.export_id}")
        assert response.status_code == 200, response.text
        match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
        assert match is not None
        return match.group(1)

    def download(self, csrf: str | None = None) -> httpx.Response:
        token = csrf if csrf is not None else self.csrf()
        return self.client.post(f"{PAGE}/{self.db.export_id}/csv", headers=FORM, content=f"csrf_token={token}")


def test_the_page_shows_the_request_without_anything_personal() -> None:
    harness = Harness().sign_in()
    page = harness.client.get(f"{PAGE}/{harness.db.export_id}")
    assert page.status_code == 200
    assert "No compraron." in page.text and "Sin quienes pidieron no recibir más mensajes." in page.text
    assert "Bajar el CSV" in page.text and "<strong>1</strong>" in page.text
    for text in PERSONAL + ("U0PIDEALAE1",):
        assert text not in page.text
    # Leer la pagina no lee personas.
    assert [name for name, _ in harness.db.export_calls] == ["approver_read_contact_export_v1"]


def test_the_csv_comes_out_once_and_is_marked_after_it_is_built() -> None:
    harness = Harness().sign_in()
    response = harness.download()
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == 'attachment; filename="contactos-20261126-0910.csv"'
    assert response.headers["cache-control"].startswith("no-store")
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert rows[0] == list(ce.DEFAULT_COLUMNS) and [row[1] for row in rows[1:]] == ["'+573000000003"]
    names = [name for name, _ in harness.db.export_calls]
    assert names[-2:] == ["approver_read_contact_export_people_v1", "approver_mark_contact_export_downloaded_v1"]
    assert harness.db.export_calls[-1][1]["p_count"] == 1
    # Segunda vez: la pagina dice que ya se bajo, y la bajada no lee a nadie.
    page = harness.client.get(f"{PAGE}/{harness.db.export_id}")
    assert "Ya se bajó" in page.text and "Bajar el CSV" not in page.text
    before = len(harness.db.export_calls)
    again = harness.client.post(f"{PAGE}/{harness.db.export_id}/csv", headers=FORM,
                                content=f"csrf_token={re.search(r'name=.csrf_token. value=.([0-9a-f]+)', page.text).group(1)}")
    assert again.status_code == 409 and "ya se bajó" in again.text
    assert "approver_read_contact_export_people_v1" not in [n for n, _ in harness.db.export_calls[before:]]


def test_an_expired_request_does_not_come_out() -> None:
    harness = Harness().sign_in()
    csrf = harness.csrf()
    harness.db.export["state"] = "vencido"
    response = harness.download(csrf)
    assert response.status_code == 410 and "30 minutos" in response.text
    assert "approver_mark_contact_export_downloaded_v1" not in [n for n, _ in harness.db.export_calls]


def test_if_it_is_marked_by_someone_else_in_between_nothing_comes_out() -> None:
    harness = Harness().sign_in()
    csrf = harness.csrf()
    harness.db.race = True
    response = harness.download(csrf)
    assert response.status_code == 409 and "+573000000003" not in response.text


def test_without_the_labels_whole_no_csv() -> None:
    harness = Harness(chatwoot=FakeChatwoot(failure=RuntimeError("conversation_scan_incomplete"))).sign_in()
    response = harness.download()
    assert response.status_code == 503 and "etiquetas" in response.text
    assert "approver_mark_contact_export_downloaded_v1" not in [n for n, _ in harness.db.export_calls]


def test_only_an_approver_with_the_csrf_and_the_origin_downloads() -> None:
    harness = Harness()
    # Sin sesion: la pagina pide entrar.
    assert harness.client.get(f"{PAGE}/{harness.db.export_id}").status_code == 401
    harness.sign_in(OUTSIDER)
    assert harness.client.get(f"{PAGE}/{harness.db.export_id}").status_code == 401
    harness.sign_in()
    assert harness.download("0" * 64).status_code in (403, 422)
    other_origin = harness.client.post(f"{PAGE}/{harness.db.export_id}/csv",
                                       headers={**FORM, "Origin": "https://otro.example.test"},
                                       content=f"csrf_token={harness.csrf()}")
    assert other_origin.status_code == 403
    assert harness.client.get(f"{PAGE}/no-es-uuid").status_code == 404
    assert harness.client.get(f"{PAGE}/{uuid.uuid4()}").status_code == 404
    assert "approver_mark_contact_export_downloaded_v1" not in [n for n, _ in harness.db.export_calls]


def test_the_expiry_shown_is_thirty_minutes() -> None:
    assert ce.EXPIRES_MINUTES == 30
    created = datetime.fromisoformat(ExportDb().export["created_at"])
    assert datetime.fromisoformat(ExportDb().export["expires_at"]) - created == timedelta(minutes=30)
