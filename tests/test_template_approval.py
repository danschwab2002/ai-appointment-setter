"""La pagina de una solicitud de plantilla y el envio a Meta (F-146 parte 3).

Los dobles de tests/test_flow_approval_page.py (la app con el cliente real de
la base, PostgREST y Slack emulados) y el cliente real de Meta contra un
servidor falso. Las cinco funciones nuevas del aprobador contestan en el
emulador con las reglas de 20261102000200; lo que la base hace de verdad lo
prueba PGlite (validate_meta_template_requests.mjs). **Regla 2:** la plantilla
y el pedido salen del validador real con una plantilla real de ATT1; lo no
capturado de Meta queda declarado en
tests/fixtures/meta_graph_templates_documented_20261108.json.
"""

from __future__ import annotations

import base64
import copy
import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from test_flow_approval_page import (
    BRAYAN,
    CLIENT_ID,
    CLIENT_SECRET,
    DAN,
    DB_KEY,
    ORIGIN,
    OUTSIDER,
    PROPOSAL,
    INSTANCE,
    FakeChatwoot,
    PostgrestEmulator,
    SlackEmulator,
    _claims,
    _Clock,
    _NoSupabase,
    _settings,
    _sign_in,
)

from bridge import flow_approval, template_request, template_request_api
from bridge.app import Settings, create_app
from bridge.instance_manifest import InstanceManifest
from bridge.meta_templates import MetaTemplatesClient, body_of, content_of

FIXTURES = Path(__file__).parent / "fixtures"
DOCUMENTED = json.loads((FIXTURES / "meta_graph_templates_documented_20261108.json").read_text(encoding="utf-8"))
REAL = {row["template"]["nombre"]: row["template"] for row in json.loads(
    (FIXTURES / "meta_template_requests" / "plantillas_reales_att1_maribel_20261102.json").read_text(
        encoding="utf-8"))["plantillas"]}
REQUEST = "aaaaaaaa-1111-4222-8333-444444444444"
PAGE = f"/flow-approval/t/{REQUEST}"
FINGERPRINT = "e" * 64
TOKEN = "EAAtoken-solo-de-plantillas-" + "x" * 40
WABA = "1234567890123"
FORM = {"Content-Type": "application/x-www-form-urlencoded", "Origin": ORIGIN}
_ATT1 = FIXTURES / "instances" / "att1"


def _jwt(claims: dict) -> str:
    part = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{part({'alg': 'RS256', 'kid': 'k'})}.{part(claims)}.firma"


ENABLED = {
    "meta_templates_enabled": True, "meta_templates_access_token": TOKEN, "meta_waba_id": WABA,
    "automation_expert_change_enabled": True, "automation_expert_change_token": "c" * 48,
    "automation_expert_change_db_key": _jwt({"role": "automation_expert_change"}),
    "automation_expert_read_enabled": True, "automation_expert_read_token": "r" * 48,
}


def _validated_row(name: str = "att1_pago_efectivo_confirmado_02", *, source: str = "att1_pago_efectivo_confirmado_01",
                   **overrides: object) -> dict:
    """Una solicitud validada: el borrador real de ATT1 pasado por el validador real, con la forma de la vista."""
    manifest = InstanceManifest.from_toml_file(_ATT1 / "instancia.toml")
    context = template_request.context_from_manifest(manifest)
    raw = copy.deepcopy(REAL[source])
    raw["nombre"] = name
    draft = template_request.parse_template(raw)
    row = {
        "request_id": REQUEST, "status": "validada", "effective_status": "validada", "revision": 1,
        "template_name": name, "language": draft.language, "category_requested": draft.category,
        "draft": draft.to_json(), "purpose": "avisar que se confirmo el pago en efectivo",
        "proposal_id": PROPOSAL, "step_pointer": "/items/0/definicion/pasos/1",
        "validation": {"meta_request": template_request.meta_request(draft, context),
                       "preview": {"burbuja": template_request.bubble(draft, context)},
                       "result": {"blocks": [], "warnings": []}},
        "fingerprint": FINGERPRINT, "validated_at": "2026-11-08T12:00:00Z", "expires_at": "2026-11-09T12:00:00Z",
        "approved_at": None, "approved_by_kind": None, "approved_by_ref": None, "sent_at": None,
        "send_attempts": 0, "last_send_error": None, "meta_template_id": None, "meta_status": None,
        "meta_category": None, "recategorized": False, "meta_rejected_reason": None, "meta_answered_at": None,
        "in_catalog_at": None, "closed_at": None, "closed_by_kind": None, "created_at": "2026-11-08T11:59:00Z",
        "events": [],
    }
    row.update(overrides)
    return row


class TemplateDb(PostgrestEmulator):
    """Las funciones de la pagina, con las reglas de 20261102000200 que le importan."""

    def __init__(self) -> None:
        super().__init__()
        self.row = _validated_row()
        self.template_calls: list[tuple[str, dict]] = []
        self.sent_in_last_day = 0
        self.send_in_progress = False
        self.record_down = False
        self.actors: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[1]
        body = json.loads(request.content)
        if "meta_template" not in name:
            return super().handler(request)
        self.calls.append((name, body))
        self.template_calls.append((name, body))
        session = self._session(body)
        if session is None:
            return self._raise("28000", "flow_approval_session_invalid", 403)
        if body["p_request_id"] != REQUEST:
            return self._raise("P0002", "meta_template_request_not_found", 500)
        return getattr(self, "_" + name)(body, session["user"])

    def _approver_get_meta_template_request_v1(self, _: dict, __: str) -> httpx.Response:
        return httpx.Response(200, json=copy.deepcopy(self.row))

    def _approver_approve_meta_template_request_v1(self, body: dict, user: str) -> httpx.Response:
        row = self.row
        if row["effective_status"] == "vencida":
            return httpx.Response(200, json={"ok": False, "reason_code": "meta_template_request_expired"})
        if body["p_fingerprint"] != row["fingerprint"]:
            return self._raise("22023", "meta_template_fingerprint_mismatch")
        if row["status"] in ("aprobada", "enviada", "en_revision", "aprobada_meta", "rechazada", "en_catalogo"):
            return httpx.Response(200, json={"ok": True, "replayed": True, "status": row["status"]})
        if row["status"] != "validada":
            return httpx.Response(200, json={"ok": False, "reason_code": "meta_template_not_validated"})
        row.update(status="aprobada", effective_status="aprobada", approved_at="2026-11-08T13:00:00Z",
                   approved_by_kind="slack", approved_by_ref=user)
        self.actors.append(user)
        return httpx.Response(200, json={"ok": True, "replayed": False, "status": "aprobada"})

    def _approver_begin_meta_template_send_v1(self, body: dict, _: str) -> httpx.Response:
        row = self.row
        if row["status"] == "enviada":
            if self.send_in_progress:
                return httpx.Response(200, json={"ok": False, "reason_code": "meta_template_send_in_progress"})
            if row["send_attempts"] >= 5:
                return httpx.Response(200, json={"ok": False, "reason_code": "meta_template_send_attempts_exhausted"})
            row["send_attempts"] += 1
        elif row["status"] == "aprobada":
            if self.sent_in_last_day >= body["p_daily_cap"]:
                return httpx.Response(200, json={"ok": False, "reason_code": "meta_template_daily_cap"})
            row.update(status="enviada", effective_status="enviada", sent_at="2026-11-08T13:00:01Z", send_attempts=1)
        else:
            return httpx.Response(200, json={"ok": False, "reason_code": "meta_template_not_approved"})
        return httpx.Response(200, json={"ok": True, "status": "enviada", "attempt": row["send_attempts"],
                                         "template_name": row["template_name"], "language": row["language"],
                                         "meta_request": row["validation"]["meta_request"]})

    def _approver_record_meta_template_sent_v1(self, body: dict, _: str) -> httpx.Response:
        if self.record_down:
            return httpx.Response(500, json={"message": "caida"})
        row = self.row
        if body["p_error_code"] is not None:
            row["last_send_error"] = body["p_error_code"]
            return httpx.Response(200, json={"ok": True, "status": "enviada", "send_failed": True})
        status = {"PENDING": "en_revision", "IN_APPEAL": "en_revision", "APPROVED": "aprobada_meta",
                  "REJECTED": "rechazada"}[body["p_meta_status"]]
        row.update(status=status, effective_status=status, meta_template_id=body["p_meta_template_id"],
                   meta_status=body["p_meta_status"], meta_category=body["p_meta_category"], last_send_error=None,
                   recategorized=body["p_meta_category"] != row["category_requested"])
        return httpx.Response(200, json={"ok": True, "replayed": False, "status": status,
                                         "recategorized": row["recategorized"]})

    def _approver_discard_meta_template_request_v1(self, body: dict, _: str) -> httpx.Response:
        row = self.row
        if row["status"] not in ("borrador", "validada", "aprobada"):
            return httpx.Response(200, json={"ok": False, "reason_code": "meta_template_request_final",
                                             "status": row["status"]})
        row.update(status="descartada", effective_status="descartada", close_reason=body["p_reason"])
        return httpx.Response(200, json={"ok": True, "replayed": False})

    def writes_of(self, name: str) -> list[dict]:
        return [body for called, body in self.template_calls if called == name]


class GraphServer:
    """La Graph API falsa: la lista y el crear, con la forma de la referencia de Meta."""

    def __init__(self) -> None:
        self.templates: list[dict] = []
        self.requests: list[httpx.Request] = []
        self.create_answer: httpx.Response | None = None
        self.down_after_creating = False

    @property
    def creates(self) -> list[httpx.Request]:
        return [request for request in self.requests if request.method == "POST"]

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in str(request.url)
        assert request.url.path == f"/v23.0/{WABA}/message_templates"
        self.requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"data": self.templates, "paging": {"cursors": {"after": "fin"}}})
        if self.create_answer is not None:
            answer, self.create_answer = self.create_answer, None
            return answer
        body = json.loads(request.content)
        created = {"id": "4596630157247420", "status": "PENDING", "category": body["category"]}
        self.templates.append({**created, "name": body["name"], "language": body["language"],
                               "components": body["components"]})
        if self.down_after_creating:
            self.down_after_creating = False
            raise httpx.ReadTimeout("sin respuesta", request=request)
        return httpx.Response(200, json=created)


def _app(db: TemplateDb | None = None, graph: GraphServer | None = None, *,
         built_meta: list[int] | None = None,
         **overrides: object) -> tuple[TestClient, TemplateDb, GraphServer, SlackEmulator, _Clock]:
    db = db or TemplateDb()
    graph = graph or GraphServer()
    slack = SlackEmulator()
    clock = _Clock()
    store = flow_approval.ApprovalStore(base_url="https://att1-rest.internal", db_key=DB_KEY,
                                        transport=httpx.MockTransport(db.handler))
    oidc = flow_approval.SlackIdTokenClient(client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                                            transport=httpx.MockTransport(slack.handler), clock=lambda: clock.now)
    settings = _settings(**{**ENABLED, **overrides})
    meta = None
    if built_meta is None:
        meta = MetaTemplatesClient(access_token=TOKEN, waba_id=WABA, transport=httpx.MockTransport(graph.handler))
    app = create_app(settings, supabase_client=_NoSupabase(),  # type: ignore[arg-type]
                     chatwoot_client=FakeChatwoot(),  # type: ignore[arg-type]
                     flow_approval_store=store, flow_approval_oidc=oidc, meta_templates_client=meta)
    return TestClient(app, base_url=ORIGIN), db, graph, slack, clock


def _signed(db: TemplateDb | None = None, graph: GraphServer | None = None, *, user: str = DAN,
            **overrides: object) -> tuple[TestClient, TemplateDb, GraphServer]:
    client, db, graph, slack, clock = _app(db, graph, **overrides)
    _sign_in(client, slack, clock, user=user)
    return client, db, graph


def _page(client: TestClient, path: str = PAGE) -> str:
    response = client.get(path)
    assert response.status_code == 200, response.text
    return response.text


def _visible(page: str) -> str:
    """Lo que se ve: sin los atributos de los tags, los estilos ni el contenido plegado."""
    page = re.sub(r"<style.*?</style>", " ", page, flags=re.S)
    page = re.sub(r"<details.*?</details>", " ", page, flags=re.S)
    return re.sub(r"<[^>]+>", " ", page)


def _field(page: str, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]+)"', page)
    assert match is not None, name
    return match.group(1)


def _post(client: TestClient, path: str, fields: dict[str, str], *, origin: str = ORIGIN) -> httpx.Response:
    return client.post(path, content=urlencode(fields), headers={**FORM, "Origin": origin}, follow_redirects=False)


def _send_fields(page: str) -> dict[str, str]:
    return {"csrf_token": _field(page, "csrf_token"), "fingerprint": _field(page, "fingerprint")}


def _send(client: TestClient) -> httpx.Response:
    return _post(client, f"{PAGE}/send", _send_fields(_page(client)))


def _done(response: httpx.Response) -> str:
    assert response.status_code == 303, response.text
    query = {key: values[0] for key, values in parse_qs(urlsplit(response.headers["location"]).query).items()}
    assert re.fullmatch(r"[0-9a-f]{64}", query.pop("f", ""))
    return query["hecho"]


# ------------------------------------------------------------------- el flag


def test_with_the_flag_off_nothing_exists_and_nothing_calls_meta() -> None:
    client, db, graph, _, _ = _app(meta_templates_enabled=False)
    assert client.get(PAGE).status_code == 404
    assert client.post(f"{PAGE}/send", headers=FORM, content="").status_code == 404
    assert client.post(f"{PAGE}/discard", headers=FORM, content="").status_code == 404
    assert client.get("/flow-approval/auth/slack/start", params={"t": REQUEST},
                      follow_redirects=False).status_code == 404
    assert graph.requests == [] and db.template_calls == []


def test_with_the_flag_off_the_client_of_meta_is_not_even_built(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(**_: object) -> None:
        raise AssertionError("con el flag apagado no se crea el cliente de Meta")

    monkeypatch.setattr("bridge.app.MetaTemplatesClient", refuse)
    client, _, graph, _, _ = _app(built_meta=[], meta_templates_enabled=False)
    assert client.get(PAGE).status_code == 404 and graph.requests == []


@pytest.mark.parametrize(("override", "message"), [
    ({"meta_templates_access_token": None}, "META_TEMPLATES_ACCESS_TOKEN"),
    ({"meta_templates_access_token": "corto"}, "META_TEMPLATES_ACCESS_TOKEN"),
    ({"meta_waba_id": None}, "META_WABA_ID"),
    ({"meta_waba_id": "no-numero"}, "META_WABA_ID"),
    ({"meta_graph_api_version": "latest"}, "META_GRAPH_API_VERSION"),
    ({"meta_templates_daily_cap": 0}, "META_TEMPLATES_DAILY_CAP"),
    ({"meta_templates_daily_cap": 21}, "META_TEMPLATES_DAILY_CAP"),
])
def test_with_the_flag_on_it_does_not_start_without_its_configuration(override: dict, message: str) -> None:
    settings = _settings(**{**ENABLED, **override})
    with pytest.raises(ValueError, match=message):
        template_request_api.check_settings(settings)


def test_the_token_cannot_be_the_same_as_another_secret() -> None:
    settings = _settings(**{**ENABLED, "automation_expert_read_token": TOKEN})
    with pytest.raises(ValueError, match="must differ"):
        template_request_api.check_settings(settings)


def test_without_a_dataclass_the_clash_check_does_not_get_in_the_way() -> None:
    template_request_api.check_settings(SimpleNamespace(
        meta_templates_enabled=True, automation_expert_change_enabled=True, flow_approval_enabled=True,
        instance_manifest=object(), meta_templates_access_token=TOKEN, meta_waba_id=WABA,
        meta_graph_api_version="v23.0", meta_templates_daily_cap=5))


@pytest.mark.parametrize(("raw", "expected"), [("", 5), ("  ", 5), ("1", 1), ("20", 20)])
def test_the_daily_cap_of_the_environment(raw: str, expected: int) -> None:
    assert template_request_api.parse_daily_cap(raw) == expected


@pytest.mark.parametrize("raw", ["0", "21", "cinco", "-3"])
def test_a_daily_cap_out_of_range_does_not_start(raw: str) -> None:
    with pytest.raises(ValueError):
        template_request_api.parse_daily_cap(raw)


# ------------------------------------------------------------------- entrar


def test_the_link_asks_to_sign_in_and_comes_back_to_the_template() -> None:
    client, db, _, slack, clock = _app()
    page = client.get(PAGE)
    assert page.status_code == 401 and f"auth/slack/start?t={REQUEST}" in page.text
    started = client.get("/flow-approval/auth/slack/start", params={"t": REQUEST}, follow_redirects=False)
    query = {key: values[0] for key, values in parse_qs(urlsplit(started.headers["location"]).query).items()}
    # La base solo acepta volver a la lista o a una propuesta: la plantilla viaja adentro del state.
    [state] = db.states.values()
    assert state["return_path"] == "/flow-approval/"
    _claims(slack, clock, query["nonce"])
    back = client.get("/flow-approval/auth/slack/callback", params={"code": "c", "state": query["state"]},
                      follow_redirects=False)
    assert (back.status_code, back.headers["location"]) == (303, PAGE)
    assert client.get(PAGE).status_code == 200


def test_a_link_that_is_not_a_uuid_or_mixes_both_targets_is_refused() -> None:
    client, *_ = _app()
    assert client.get("/flow-approval/t/no-es-un-id").status_code == 404
    for params in ({"t": "no-es-un-id"}, {"t": REQUEST, "p": PROPOSAL}):
        assert client.get("/flow-approval/auth/slack/start", params=params, follow_redirects=False).status_code == 404


def test_a_request_the_base_does_not_find_for_this_instance_is_404() -> None:
    # Que la base no devuelva la fila de otra instancia es de la funcion SQL (filtra por p_instance). Aca se prueba
    # lo del bridge: pide con la instancia del manifiesto y un «no existe» de la base es un 404.
    client, db, _ = _signed()
    other = "bbbbbbbb-1111-4222-8333-444444444444"
    assert client.get(f"/flow-approval/t/{other}").status_code == 404
    assert [(body["p_request_id"], body["p_instance"]) for _, body in db.template_calls] == [(other, INSTANCE)]


def test_who_was_taken_off_the_list_loses_the_session_and_cannot_send() -> None:
    client, db, graph = _signed(user=BRAYAN)
    fields = _send_fields(_page(client))
    [session] = db.sessions.values()
    session["user"] = OUTSIDER
    response = _post(client, f"{PAGE}/send", fields)
    assert response.status_code == 401
    assert client.get(PAGE).status_code == 401
    assert graph.requests == [] and not db.writes_of("approver_approve_meta_template_request_v1")


def test_without_a_session_there_is_no_send() -> None:
    client, db, graph, _, _ = _app()
    response = _post(client, f"{PAGE}/send", {"csrf_token": "0" * 64, "fingerprint": FINGERPRINT})
    assert response.status_code == 401
    assert graph.requests == [] and db.template_calls == []


def test_another_origin_or_a_bad_csrf_do_not_send() -> None:
    client, db, graph = _signed()
    fields = _send_fields(_page(client))
    assert _post(client, f"{PAGE}/send", fields, origin="https://otra.example").status_code == 403
    assert _post(client, f"{PAGE}/send", {**fields, "csrf_token": "0" * 64}).status_code == 403
    assert _post(client, f"{PAGE}/send", {**fields, "fingerprint": "corta"}).status_code == 422
    assert graph.requests == [] and not db.writes_of("approver_approve_meta_template_request_v1")


# ------------------------------------------------------------------- lo que se ve


def test_the_page_is_in_business_language_with_the_bubble_and_no_raw_ids() -> None:
    client, db, graph = _signed()
    page = _page(client)
    shown = _visible(page)
    assert "Pedido a Meta" in shown and "plantilla nueva de" in shown and "utilidad" in shown
    assert "Bienvenida de" in shown and db.row["validation"]["preview"]["burbuja"]["cuerpo"][:30] in page
    assert "Se manda a Meta. Nadie recibe nada todavía." in shown
    assert "Utilidad: sobre algo que la persona ya hizo" in shown
    assert 'class="button primary">Mandar a Meta</button>' in page and ">Descartar</button>" in page
    # Nada que no deba salir en una captura.
    for raw in (REQUEST, FINGERPRINT, DAN, PROPOSAL, db.row["template_name"], "att1/"):
        assert raw not in shown, raw
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-", shown)
    assert "Entraste con Slack" in shown
    assert graph.requests == []  # mirar no llama a Meta


def test_the_warnings_are_said_in_words_with_the_validators_codes_out_of_sight() -> None:
    db = TemplateDb()
    db.row["validation"]["result"]["warnings"] = [{"code": "template_utility_sounds_promotional", "path": "/categoria",
                                                   "reason": "x"}]
    client, _, _ = _signed(db)
    shown = _visible(_page(client))
    assert "Va como utilidad y habla de descuento" in shown and "template_utility" not in shown


def test_every_text_of_the_model_is_escaped() -> None:
    db = TemplateDb()
    attack = "<script>alert(1)</script>"
    db.row["validation"]["preview"]["burbuja"].update(
        encabezado=f"H{attack}", cuerpo=f"C{attack}", pie=f"P{attack}",
        botones=[{"tipo": "url_fija", "texto": f"B{attack}", "url": f'https://x.example/"><script>alert(2)</script>'}])
    db.row["validation"]["result"]["warnings"] = [{"code": "aviso_desconocido", "path": "/x", "reason": f"W{attack}"}]
    db.row["purpose"] = f"F{attack}"
    db.proposal["items"][0]["flujo"] = f"flujo{attack}"
    client, _, _ = _signed(db)
    page = _page(client)
    assert "<script>" not in page
    # Cada texto llego a la pagina, escapado: si alguno no se mostrara, la prueba no probaria nada.
    escaped = "&lt;script&gt;alert(1)&lt;/script&gt;"
    for text in (f"H{escaped}", f"C{escaped}", f"P{escaped}", f"B{escaped}", f"W{escaped}", f"F{escaped}",
                 f"Flujo{escaped}"):
        assert text in page, text
    assert "https://x.example/&quot;&gt;&lt;script&gt;alert(2)&lt;/script&gt;" in page


def test_a_marketing_template_says_what_it_implies() -> None:
    db = TemplateDb()
    db.row["category_requested"] = "MARKETING"
    db.row["validation"]["preview"]["burbuja"]["botones"] = [{"tipo": "respuesta", "texto": "No más mensajes"}]
    client, _, _ = _signed(db)
    shown = _visible(_page(client))
    assert "Marketing: invita a comprar" in shown and "da de baja a la persona" in shown


@pytest.mark.parametrize(("state", "extra", "words"), [
    ("en_revision", {"sent_at": "2026-11-08T13:00:00Z"}, "Meta la está revisando"),
    ("aprobada_meta", {}, "Meta la aprobó"),
    ("rechazada", {"meta_rejected_reason": "PROMOTIONAL"}, "Vende, y estaba pedida como utilidad."),
    ("en_catalogo", {}, "Ya se puede usar en un flujo"),
    ("vencida", {}, "Pasaron las 24 horas"),
    ("descartada", {}, "No se mandó a Meta"),
])
def test_after_it_left_there_is_no_send_button_and_the_state_is_in_words(state: str, extra: dict, words: str) -> None:
    db = TemplateDb()
    db.row.update(status=state, effective_status=state, **extra)
    client, _, _ = _signed(db)
    page = _page(client)
    assert words in _visible(page)
    assert "Mandar a Meta</button>" not in page and "Volver a mandar" not in page and ">Descartar</button>" not in page


def test_a_recategorized_template_says_so() -> None:
    db = TemplateDb()
    db.row.update(status="aprobada_meta", effective_status="aprobada_meta", recategorized=True,
                  meta_category="MARKETING")
    client, _, _ = _signed(db)
    assert "Meta la aprobó como marketing, otra categoría que la pedida." in _visible(_page(client))


def test_after_sending_the_name_to_search_is_folded_out_of_the_capture() -> None:
    db = TemplateDb()
    db.row.update(status="en_revision", effective_status="en_revision", sent_at="2026-11-08T13:00:00Z")
    client, _, _ = _signed(db)
    page = _page(client)
    assert "<details" in page and db.row["template_name"] in page
    assert db.row["template_name"] not in _visible(page)


# ------------------------------------------------------------------- mandar


def test_sending_approves_begins_looks_for_the_name_creates_and_records() -> None:
    client, db, graph = _signed()
    frozen = copy.deepcopy(db.row["validation"]["meta_request"])
    response = _send(client)
    assert _done(response) == "enviada"
    assert [name for name, _ in db.template_calls if name != "approver_get_meta_template_request_v1"] == [
        "approver_approve_meta_template_request_v1", "approver_begin_meta_template_send_v1",
        "approver_record_meta_template_sent_v1"]
    # Lo que llega a Meta es el pedido congelado, tal cual: primero lista, despues crea.
    assert [request.method for request in graph.requests] == ["GET", "POST"]
    assert json.loads(graph.creates[0].content) == frozen
    [recorded] = db.writes_of("approver_record_meta_template_sent_v1")
    assert (recorded["p_meta_template_id"], recorded["p_meta_status"], recorded["p_meta_category"],
            recorded["p_error_code"]) == ("4596630157247420", "PENDING", frozen["category"], None)
    [begun] = db.writes_of("approver_begin_meta_template_send_v1")
    assert begun["p_daily_cap"] == 5 and begun["p_fingerprint"] == FINGERPRINT
    assert db.row["status"] == "en_revision" and db.actors == [DAN]
    assert "approver_ref" not in json.dumps(recorded) and DAN not in json.dumps(begun)
    assert "Listo: se mandó a Meta" in _page(client, response.headers["location"])


def test_two_clicks_send_one_template() -> None:
    client, db, graph = _signed()
    fields = _send_fields(_page(client))
    assert _done(_post(client, f"{PAGE}/send", fields)) == "enviada"
    assert _done(_post(client, f"{PAGE}/send", fields)) == "ya_enviada"
    assert len(graph.creates) == 1
    assert len(db.writes_of("approver_record_meta_template_sent_v1")) == 1
    assert len(db.actors) == 1


def test_a_click_while_another_send_is_in_course_does_not_call_meta() -> None:
    db = TemplateDb()
    db.row.update(status="enviada", effective_status="enviada", send_attempts=1, sent_at="2026-11-08T13:00:00Z")
    db.send_in_progress = True
    client, _, graph = _signed(db)
    response = _send(client)
    assert _done(response) == "meta_template_send_in_progress"
    assert graph.requests == []
    assert "Hay un envío en curso" in _page(client, response.headers["location"])


def test_when_meta_does_not_answer_the_retry_finds_it_and_does_not_create_another() -> None:
    graph = GraphServer()
    graph.down_after_creating = True
    client, db, graph = _signed(graph=graph)
    first = _send(client)
    assert _done(first) == "meta_unreachable"
    assert db.row["status"] == "enviada" and db.row["last_send_error"] == "meta_unreachable"
    page = _page(client, first.headers["location"])
    assert "Meta no contestó" in page and "Volver a mandar</button>" in page
    assert "meta_unreachable" not in _visible(page)
    second = _send(client)
    assert _done(second) == "enviada"
    assert len(graph.creates) == 1, "el reintento encontro la que ya se habia creado"
    assert db.row["status"] == "en_revision" and db.row["meta_template_id"] == "4596630157247420"


def test_the_name_with_another_text_is_not_taken_as_ours() -> None:
    graph = GraphServer()
    db = TemplateDb()
    frozen = db.row["validation"]["meta_request"]
    other = copy.deepcopy(frozen["components"])
    assert body_of(other) is not None
    next(component for component in other if component["type"] == "BODY")["text"] = "Otro texto de otra plantilla."
    graph.templates.append({"id": "99", "status": "APPROVED", "category": frozen["category"], "name": frozen["name"],
                            "language": frozen["language"], "components": other})
    client, db, graph = _signed(db, graph)
    response = _send(client)
    assert _done(response) == "meta_name_taken_other_text"
    assert graph.creates == [] and db.row["meta_template_id"] is None
    assert db.writes_of("approver_record_meta_template_sent_v1")[0]["p_error_code"] == "meta_name_taken_other_text"
    assert "otro contenido" in _page(client, response.headers["location"])


def _marketing_with_opt_out() -> str:
    return next(name for name, template in REAL.items() if template["categoria"] == "MARKETING" and any(
        button.get("texto") == "No más mensajes" for button in template.get("botones", [])))


def _without_the_opt_out(components: list[dict]) -> None:
    buttons = next(component for component in components if component["type"] == "BUTTONS")
    buttons["buttons"] = [button for button in buttons["buttons"] if button.get("text") != "No más mensajes"]


def _other_header(components: list[dict]) -> None:
    components.insert(0, {"type": "HEADER", "format": "TEXT", "text": "Encabezado ajeno"})


def _other_footer(components: list[dict]) -> None:
    components.insert(-1, {"type": "FOOTER", "text": "Pie ajeno"})


def _other_button_text(components: list[dict]) -> None:
    next(component for component in components if component["type"] == "BUTTONS")["buttons"][0]["text"] = "Otro"


def _other_button_url(components: list[dict]) -> None:
    buttons = next(component for component in components if component["type"] == "BUTTONS")["buttons"]
    next(button for button in buttons if button["type"] == "URL")["url"] = "https://otro.example/{{1}}"


@pytest.mark.parametrize("change", [_without_the_opt_out, _other_header, _other_footer, _other_button_text,
                                    _other_button_url])
def test_same_name_and_body_with_other_content_is_not_adopted(change) -> None:
    name = _marketing_with_opt_out()
    db = TemplateDb()
    db.row = _validated_row(f"{name[:-2]}99", source=name)
    frozen = db.row["validation"]["meta_request"]
    components = copy.deepcopy(frozen["components"])
    change(components)
    assert body_of(components) == body_of(frozen["components"])
    assert content_of(components) != content_of(frozen["components"])
    graph = GraphServer()
    graph.templates.append({"id": "99", "status": "APPROVED", "category": frozen["category"], "name": frozen["name"],
                            "language": frozen["language"], "components": components})
    client, db, graph = _signed(db, graph)
    assert _done(_send(client)) == "meta_name_taken_other_text"
    assert graph.creates == [] and db.row["meta_template_id"] is None


def test_the_content_of_a_real_approved_template_is_what_the_bridge_would_send() -> None:
    # Regla 2: att1_bienvenida_01 tal como Meta la aprobo (catalogo de la bandeja 11, 2026-10-26) contra el pedido que
    # arma el validador con su borrador: el reintento tiene que reconocer lo que Meta devuelve.
    captured = json.loads((FIXTURES / "chatwoot_message_templates_inbox_11_bienvenida_20261026.json").read_text(
        encoding="utf-8"))["message_templates"][0]
    context = template_request.context_from_manifest(InstanceManifest.from_toml_file(_ATT1 / "instancia.toml"))
    request = template_request.meta_request(template_request.parse_template(copy.deepcopy(REAL["att1_bienvenida_01"])),
                                            context)
    assert content_of(captured["components"]) == content_of(request["components"]) is not None


def test_a_paused_template_with_the_name_is_not_adopted_either() -> None:
    graph = GraphServer()
    db = TemplateDb()
    frozen = db.row["validation"]["meta_request"]
    graph.templates.append({"id": "99", "status": "PAUSED", "category": frozen["category"], "name": frozen["name"],
                            "language": frozen["language"], "components": frozen["components"]})
    client, db, graph = _signed(db, graph)
    assert _done(_send(client)) == "meta_name_taken_unusable"
    assert graph.creates == [] and db.row["status"] == "enviada"


@pytest.mark.parametrize(("key", "status", "code", "words"), [
    ("error_expired_token", 401, "meta:190", "venció o la revocaron"),
    ("error_permission", 403, "meta:10", "no tiene permiso"),
    ("error_rate_limit", 429, "meta:4", "Meta pidió esperar"),
    ("error_limit_templates", 400, "meta:2388019", "tope de plantillas"),
    ("error_body_too_long", 400, "meta:100:2388040", "supera el largo"),
])
def test_an_error_of_meta_is_recorded_and_translated(key: str, status: int, code: str, words: str,
                                                     caplog: pytest.LogCaptureFixture) -> None:
    graph = GraphServer()
    graph.create_answer = httpx.Response(status, json=DOCUMENTED[key])
    client, db, graph = _signed(graph=graph)
    with caplog.at_level(logging.DEBUG):
        response = _send(client)
    assert _done(response) == code
    assert db.row["status"] == "enviada" and db.row["last_send_error"] == code
    [recorded] = db.writes_of("approver_record_meta_template_sent_v1")
    assert (recorded["p_error_code"], recorded["p_meta_template_id"]) == (code, None)
    page = _visible(_page(client, response.headers["location"]))
    assert words in page and code not in page and DOCUMENTED[key]["error"]["message"] not in page
    assert TOKEN not in caplog.text and DOCUMENTED[key]["error"]["message"] not in caplog.text


def test_an_unknown_error_of_meta_says_so_without_the_code() -> None:
    graph = GraphServer()
    graph.create_answer = httpx.Response(400, json={"error": {"code": 987654, "message": "algo raro"}})
    client, db, graph = _signed(graph=graph)
    response = _send(client)
    page = _visible(_page(client, response.headers["location"]))
    assert "Quedó registrado" in page and "987654" not in page and "algo raro" not in page


def test_meta_approved_on_the_spot() -> None:
    graph = GraphServer()
    db = TemplateDb()
    graph.create_answer = httpx.Response(200, json={"id": "4596630157247420", "status": "APPROVED",
                                                    "category": db.row["category_requested"]})
    client, db, graph = _signed(db, graph)
    assert _done(_send(client)) == "meta_aprobada"
    assert db.row["status"] == "aprobada_meta"


def test_a_status_the_base_does_not_take_stays_as_an_error_not_a_lie() -> None:
    graph = GraphServer()
    graph.create_answer = httpx.Response(200, json={"id": "4596630157247420", "status": "LOCKED", "category": "UTILITY"})
    client, db, graph = _signed(graph=graph)
    assert _done(_send(client)) == "meta_unexpected_answer"
    assert db.row["meta_template_id"] is None and db.row["status"] == "enviada"


def test_if_the_answer_could_not_be_written_the_page_says_so_and_a_retry_finds_it() -> None:
    db = TemplateDb()
    db.record_down = True
    client, db, graph = _signed(db)
    first = _send(client)
    assert _done(first) == "meta_sin_registrar"
    assert "no se pudo anotar" in _page(client, first.headers["location"])
    db.record_down = False
    assert _done(_send(client)) == "enviada"
    assert len(graph.creates) == 1


def test_the_daily_cap_stops_before_meta() -> None:
    db = TemplateDb()
    db.sent_in_last_day = 3
    client, db, graph = _signed(db, meta_templates_daily_cap=3)
    response = _send(client)
    assert _done(response) == "meta_template_daily_cap"
    assert graph.requests == []
    assert db.writes_of("approver_begin_meta_template_send_v1")[0]["p_daily_cap"] == 3
    assert "tope de plantillas por día" in _page(client, response.headers["location"])


def test_the_fingerprint_that_was_shown_is_the_one_sent_and_a_changed_one_stops() -> None:
    db = TemplateDb()
    client, db, graph = _signed(db)
    fields = _send_fields(_page(client))
    db.row["fingerprint"] = "d" * 64
    response = _post(client, f"{PAGE}/send", fields)
    assert _done(response) == "meta_template_fingerprint_mismatch"
    assert graph.requests == []
    assert "La solicitud cambió" in _page(client, response.headers["location"])


def test_an_expired_request_is_not_sent_and_offers_no_button() -> None:
    db = TemplateDb()
    client, db, graph = _signed(db)
    fields = _send_fields(_page(client))
    db.row["effective_status"] = "vencida"
    assert _done(_post(client, f"{PAGE}/send", fields)) == "meta_template_request_expired"
    assert graph.requests == []
    assert "Mandar a Meta</button>" not in _page(client)


def test_five_attempts_and_there_is_no_retry_button() -> None:
    db = TemplateDb()
    db.row.update(status="enviada", effective_status="enviada", send_attempts=5, last_send_error="meta_unreachable")
    client, _, graph = _signed(db)
    page = _page(client)
    assert "Volver a mandar" not in page and "Meta no contestó" in page
    assert graph.requests == []


# ------------------------------------------------------------------- descartar


def test_discarding_writes_the_reason_and_does_not_call_meta() -> None:
    client, db, graph = _signed()
    page = _page(client)
    response = _post(client, f"{PAGE}/discard", {"csrf_token": _field(page, "csrf_token"), "motivo": "no va"})
    assert _done(response) == "descartada"
    assert db.row["status"] == "descartada" and db.row["close_reason"] == "no va"
    assert graph.requests == []


def test_discarding_without_a_reason_uses_the_default() -> None:
    client, db, _ = _signed()
    response = _post(client, f"{PAGE}/discard", {"csrf_token": _field(_page(client), "csrf_token")})
    assert _done(response) == "descartada"
    assert db.row["close_reason"] == "descartada desde la pagina de aprobacion"


def test_what_already_reached_meta_cannot_be_discarded() -> None:
    db = TemplateDb()
    db.row.update(status="en_revision", effective_status="en_revision")
    client, db, _ = _signed(db)
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', _page(client)).group(1)  # type: ignore[union-attr]
    response = _post(client, f"{PAGE}/discard", {"csrf_token": csrf, "motivo": "tarde"})
    assert _done(response) == "meta_template_request_final"
    assert db.row["status"] == "en_revision"
    assert "Ya no se puede descartar" in _page(client, response.headers["location"])


def _expire_session(db: TemplateDb) -> None:
    for session in db.sessions.values():
        session["revoked"] = True


@pytest.mark.parametrize(("action", "fields"), [
    ("send", {"csrf_token": "0" * 64, "fingerprint": FINGERPRINT}),
    ("discard", {"csrf_token": "0" * 64, "motivo": "no va"}),
])
def test_when_the_session_expired_the_login_comes_back_to_the_template(action: str, fields: dict[str, str]) -> None:
    client, db, graph = _signed()
    _expire_session(db)
    response = _post(client, f"{PAGE}/{action}", fields)
    assert response.status_code == 401
    assert f"auth/slack/start?t={REQUEST}" in response.text and "Aprobar plantillas de WhatsApp" in response.text
    assert graph.requests == []


_MINIMAL_ENVIRONMENT = {
    "CHATWOOT_WEBHOOK_SECRET": "test-secret",
    "ALLOWED_WHATSAPP_JID": "12025550123@s.whatsapp.net",
    "CHATWOOT_AGENT_BOT_ID": "1",
    "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
    "CHATWOOT_ACCOUNT_ID": "1",
    "CHATWOOT_CONTROL_API_ACCESS_TOKEN": "test-control-token",
    "CHATWOOT_PAUSE_MACRO_ID": "1",
}


def _environment(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for name, value in {**_MINIMAL_ENVIRONMENT, **values}.items():
        monkeypatch.setenv(name, value)


def test_with_the_flag_off_a_broken_daily_cap_does_not_stop_the_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    _environment(monkeypatch, META_TEMPLATES_ENABLED="false", META_TEMPLATES_DAILY_CAP="cinco")
    assert Settings.from_env().meta_templates_daily_cap == 5


def test_with_the_flag_on_the_daily_cap_is_read_and_a_broken_one_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    _environment(monkeypatch, META_TEMPLATES_ENABLED="true", META_TEMPLATES_DAILY_CAP="3")
    assert Settings.from_env().meta_templates_daily_cap == 3
    monkeypatch.setenv("META_TEMPLATES_DAILY_CAP", "cinco")
    with pytest.raises(ValueError, match="META_TEMPLATES_DAILY_CAP"):
        Settings.from_env()


def test_the_four_new_variables_are_declared_off() -> None:
    root = Path(__file__).parent.parent
    example = (root / ".env.example").read_text(encoding="utf-8").splitlines()
    compose = (root / "compose.yaml").read_text(encoding="utf-8")
    for name, default in (("META_TEMPLATES_ENABLED", "false"), ("META_TEMPLATES_ACCESS_TOKEN", ""),
                          ("META_WABA_ID", ""), ("META_GRAPH_API_VERSION", "v23.0"),
                          ("META_TEMPLATES_DAILY_CAP", "5")):
        assert f"{name}={default}" in example
        assert f"{name}: ${{{name}:-{default}}}" in compose


def test_without_a_session_there_is_no_discard() -> None:
    client, db, graph, _, _ = _app()
    response = _post(client, f"{PAGE}/discard", {"csrf_token": "0" * 64, "motivo": "no va"})
    assert response.status_code == 401
    assert db.template_calls == [] and db.row["status"] == "validada" and graph.requests == []


def test_another_origin_or_a_bad_csrf_do_not_discard() -> None:
    client, db, _ = _signed()
    csrf = _field(_page(client), "csrf_token")
    assert _post(client, f"{PAGE}/discard", {"csrf_token": csrf, "motivo": "x"},
                 origin="https://otra.example").status_code == 403
    assert _post(client, f"{PAGE}/discard", {"csrf_token": "0" * 64, "motivo": "x"}).status_code == 403
    assert not db.writes_of("approver_discard_meta_template_request_v1") and db.row["status"] == "validada"
