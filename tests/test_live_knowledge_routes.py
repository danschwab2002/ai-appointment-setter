"""Las rutas del AE y la pagina del conocimiento vivo (live-knowledge-v1, pieza 5.5).

Los dobles de ``test_agent_campaign_routes`` y ``test_flow_approval_page`` (la app con el
cliente real de la base, PostgREST y Slack emulados). El emulador sigue las reglas de
20261126600000 que le importan a estas rutas; lo que la base hace de verdad lo prueba PGlite
(``validate_agent_live_knowledge.mjs``). El conocimiento es el v1 de ATT1 del repo de la
instancia (``instances/att1``); los cambios los arma el test.
"""

from __future__ import annotations

import copy
import json
import tomllib
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from test_agent_campaign_routes import AE_KEY, ASKER, TOKEN, Chatwoot, _field, _visible
from test_flow_approval_page import ATT1 as ATT1_DIR
from test_flow_approval_page import (
    CLIENT_ID,
    CLIENT_SECRET,
    DAN,
    DB_KEY,
    ORIGIN,
    PostgrestEmulator,
    SlackEmulator,
    _Clock,
    _NoSupabase,
    _settings,
    _sign_in,
)

from bridge import flow_approval, live_knowledge, live_knowledge_api
from bridge.app import create_app
from bridge.commercial_knowledge import CommercialKnowledge

PREFIX = "/internal/automation-expert/v1/knowledge"
KNOWLEDGE_PATH = ATT1_DIR / "conocimiento" / "knowledge-v1.toml"
KNOWLEDGE = CommercialKnowledge.from_toml_file(KNOWLEDGE_PATH, require_approved=False)
RAW = tomllib.loads(KNOWLEDGE_PATH.read_text(encoding="utf-8"))
KNOWLEDGE_ID = "6f1c1e9a-55aa-4f6e-9d55-000000000055"
OTHER_ID = "6f1c1e9a-55aa-4f6e-9d55-000000000056"
FINGERPRINT = "f" * 64
PAGE = f"/flow-approval/k/{KNOWLEDGE_ID}"
FORM = {"Content-Type": "application/x-www-form-urlencoded", "Origin": ORIGIN}
ENABLED = {
    "agent_live_knowledge_enabled": True, "commercial_knowledge": KNOWLEDGE,
    "commercial_knowledge_path": KNOWLEDGE_PATH, "hermes_shadow_enabled": True,
    "automation_expert_change_enabled": True, "automation_expert_change_token": TOKEN,
    "automation_expert_change_db_key": AE_KEY,
    "automation_expert_read_enabled": True, "automation_expert_read_token": "r" * 48,
    "chatwoot_account_id": 2, "chatwoot_inbox_id": 11,
}


def _faq_change(extra: str = " Te escribimos por aca.") -> dict[str, Any]:
    faq = copy.deepcopy(RAW["faq"])
    faq[0]["respuesta"] += extra  # la mutacion es del test
    return {"faq": faq}


class Db(PostgrestEmulator):
    """Las funciones del conocimiento vivo, con las reglas de la migracion que le importan a la pagina."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []
        self.saved: list[dict[str, Any]] = []

    def live(self) -> dict[str, Any] | None:
        return next((row for row in self.rows.values() if row["status"] == "publicada"), None)

    def handler(self, request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[1]
        if "agent_knowledge" not in name:
            return super().handler(request)
        body = json.loads(request.content)
        self.calls.append(name)
        assert body["p_instance"] == {"tenant_ref": "lancemos", "ally_ref": "att1"}
        if name.startswith("approver_"):
            session = self._session(body)
            if session is None:
                return self._raise("28000", "flow_approval_session_invalid", 403)
            if "p_knowledge_id" in body and body["p_knowledge_id"] not in self.rows:
                return self._raise("P0002", "agent_knowledge_not_found", 500)
            return getattr(self, "_" + name)(body, session["user"])
        return getattr(self, "_" + name)(body)

    def _view(self, row: dict[str, Any]) -> dict[str, Any]:
        live = self.live()
        return {**copy.deepcopy(row), "effective_status": row["status"],
                "live": None if live is None else {"knowledge_id": live["knowledge_id"],
                                                   "live_version": live["live_version"]}}

    def _save_agent_knowledge_version_v1(self, body: dict) -> httpx.Response:
        live = self.live()
        if (body["p_base_origin"] == "archivo" and live is not None) or (
                body["p_base_origin"] == "vivo" and (live is None or live["knowledge_id"] != body["p_base_knowledge_id"])):
            return httpx.Response(200, json={"ok": False, "reason_code": "agent_knowledge_base_changed"})
        self.saved.append(body)
        knowledge_id = KNOWLEDGE_ID if KNOWLEDGE_ID not in self.rows else OTHER_ID
        self.rows[knowledge_id] = {
            "knowledge_id": knowledge_id, "status": "validada", "live_version": len(self.rows) + 1,
            "file_version": body["p_file_version"], "file_sha256": body["p_file_sha256"],
            "base_origin": body["p_base_origin"], "base_knowledge_id": body["p_base_knowledge_id"],
            "base_sha256": body["p_base_sha256"], "change": body["p_change"], "knowledge": body["p_knowledge"],
            "rendered_sha256": body["p_rendered_sha256"], "rendered_bytes": body["p_rendered_bytes"],
            "preview": body["p_preview"], "purpose": body["p_purpose"], "fingerprint": FINGERPRINT,
            "validated_at": "2026-11-26T12:00:00Z", "expires_at": "2026-11-27T12:00:00Z", "approved_at": None,
            "approved_by_ref": None, "closed_at": None, "created_at": "2026-11-26T12:00:00Z", "events": []}
        row = self.rows[knowledge_id]
        return httpx.Response(200, json={"ok": True, "knowledge_id": knowledge_id, "live_version": row["live_version"],
                                         "status": "validada", "fingerprint": FINGERPRINT,
                                         "expires_at": row["expires_at"]})

    def _get_agent_knowledge_version_v1(self, body: dict) -> httpx.Response:
        if body["p_knowledge_id"] not in self.rows:
            return self._raise("P0002", "agent_knowledge_not_found", 500)
        return httpx.Response(200, json=self._view(self.rows[body["p_knowledge_id"]]))

    def _list_agent_knowledge_versions_v1(self, _: dict) -> httpx.Response:
        return httpx.Response(200, json={"versions": [{"knowledge_id": key} for key in self.rows],
                                         "database_at": "2026-11-26T12:00:00Z"})

    def _approver_get_agent_knowledge_v1(self, body: dict, _: str) -> httpx.Response:
        return httpx.Response(200, json=self._view(self.rows[body["p_knowledge_id"]]))

    def _approver_publish_agent_knowledge_v1(self, body: dict, user: str) -> httpx.Response:
        row = self.rows[body["p_knowledge_id"]]
        if body["p_fingerprint"] != row["fingerprint"] or body["p_rendered_sha256"] != row["rendered_sha256"]:
            return httpx.Response(200, json={"ok": False, "reason_code": "agent_knowledge_fingerprint_mismatch"})
        if row["status"] != "validada":
            return httpx.Response(200, json={"ok": False, "reason_code": "agent_knowledge_not_validated"})
        live = self.live()
        if (row["base_origin"] == "archivo" and live is not None) or (
                row["base_origin"] == "vivo" and (live is None or live["knowledge_id"] != row["base_knowledge_id"])):
            return httpx.Response(200, json={"ok": False, "reason_code": "agent_knowledge_base_changed"})
        if live is not None:
            live.update(status="retirada", closed_at="2026-11-26T13:00:00Z")
        row.update(status="publicada", approved_at="2026-11-26T13:00:00+00:00", approved_by_ref=user)
        return httpx.Response(200, json={"ok": True, "replayed": False, "status": "publicada"})

    def _approver_discard_agent_knowledge_v1(self, body: dict, _: str) -> httpx.Response:
        row = self.rows[body["p_knowledge_id"]]
        if row["status"] != "validada":
            return httpx.Response(200, json={"ok": False, "reason_code": "agent_knowledge_final"})
        row.update(status="descartada")
        return httpx.Response(200, json={"ok": True, "replayed": False})

    def _approver_return_to_file_agent_knowledge_v1(self, _: dict, __: str) -> httpx.Response:
        live = self.live()
        if live is None:
            return httpx.Response(200, json={"ok": False, "reason_code": "agent_knowledge_no_live"})
        live.update(status="retirada")
        return httpx.Response(200, json={"ok": True, "retired_live_version": live["live_version"]})


class Base(_NoSupabase):
    """Solo lo que usa el bridge con el flag: la lectura de la version publicada."""

    def __init__(self, db: Db) -> None:
        self.__dict__["db"] = db
        self.__dict__["reads"] = 0

    async def resolve_commercial_ally_runtime_binding(self, config: object) -> object:
        return config

    async def get_live_agent_knowledge(self, instance: dict[str, str]) -> dict[str, Any]:
        self.__dict__["reads"] += 1
        live = self.db.live()
        return {"knowledge": None if live is None else {
            key: live[key] for key in ("knowledge_id", "live_version", "file_version", "file_sha256", "knowledge",
                                       "rendered_sha256", "approved_at")} | {"approved_by_ref": live["approved_by_ref"]}}


class _Processor:
    """Lo que el bridge exige del procesador con el flag: que lea la fuente viva."""

    def __init__(self) -> None:
        self.source: live_knowledge.LiveKnowledgeSource | None = None

    def bind_knowledge_source(self, source: live_knowledge.LiveKnowledgeSource | None) -> None:
        self.source = source


@pytest.fixture(autouse=True)
def _image_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SETTER_VERSION", "v1.43.0")
    monkeypatch.setenv("GIT_SHA", "abcdef0")


def _as_ae(db: Db) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {AE_KEY}"
        return db.handler(request)

    return handler


def _app(db: Db | None = None, **overrides: Any) -> tuple[TestClient, Db, SlackEmulator, _Clock, _Processor]:
    db = db or Db()
    slack = SlackEmulator()
    clock = _Clock()
    processor = _Processor()
    approval = flow_approval.ApprovalStore(base_url="https://att1-rest.internal", db_key=DB_KEY,
                                           transport=httpx.MockTransport(db.handler))
    oidc = flow_approval.SlackIdTokenClient(client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                                            transport=httpx.MockTransport(slack.handler), clock=lambda: clock.now)
    store = live_knowledge_api.KnowledgeStore(base_url="https://att1-rest.internal", db_key=AE_KEY,
                                              transport=httpx.MockTransport(_as_ae(db)))
    app = create_app(_settings(**{**ENABLED, **overrides}), shadow_processor=processor,  # type: ignore[arg-type]
                     supabase_client=Base(db), chatwoot_client=Chatwoot(),  # type: ignore[arg-type]
                     flow_approval_store=approval, flow_approval_oidc=oidc, live_knowledge_store=store)
    client = TestClient(app, base_url=ORIGIN)
    client.__enter__()
    return client, db, slack, clock, processor


def _signed(db: Db | None = None, **overrides: Any) -> tuple[TestClient, Db, _Processor]:
    client, db, slack, clock, processor = _app(db, **overrides)
    _sign_in(client, slack, clock, user=DAN)
    return client, db, processor


def _ae(client: TestClient, method: str, path: str, body: dict | None = None, *, token: str = TOKEN) -> httpx.Response:
    return client.request(method, f"{PREFIX}{path}", json=body, headers={"Authorization": f"Bearer {token}"})


def _propose(client: TestClient, change: dict[str, Any] | None = None) -> httpx.Response:
    return _ae(client, "POST", "", {"cambios": change or _faq_change(), "purpose": "Suma una linea a la primera FAQ",
                                    "requested_by": ASKER})


def _post(client: TestClient, action: str, page: str = PAGE, **fields: str) -> httpx.Response:
    return client.post(f"{page}/{action}", content=urlencode(fields), headers=FORM, follow_redirects=False)


def _done(response: httpx.Response) -> str:
    assert response.status_code == 303, response.text
    return parse_qs(urlsplit(response.headers["location"]).query)["hecho"][0]


def _publish(client: TestClient, page: str = PAGE) -> httpx.Response:
    html = client.get(page).text
    return _post(client, "publish", page, csrf_token=_field(html, "csrf_token"), fingerprint=_field(html, "fingerprint"))


# --------------------------------------------------------------- el flag apagado


def test_with_the_flag_off_nothing_exists_and_ready_does_not_change() -> None:
    client, db, _, _, processor = _app(agent_live_knowledge_enabled=False)
    assert _ae(client, "GET", "").status_code == 404
    assert _ae(client, "POST", "", {"cambios": {}}).status_code == 404
    assert client.get(PAGE, follow_redirects=False).status_code == 404
    assert "live_knowledge" not in client.get("/ready").json()
    assert processor.source is None and db.calls == []


def test_the_flag_needs_the_ae_the_page_and_the_shadow() -> None:
    for missing in ("automation_expert_change_enabled", "flow_approval_enabled", "hermes_shadow_enabled"):
        with pytest.raises(ValueError, match=live_knowledge.FLAG_ENV):
            _app(**{missing: False})
    with pytest.raises(ValueError, match="COMMERCIAL_KNOWLEDGE_ENABLED"):
        _app(commercial_knowledge=None)


# --------------------------------------------------------------- las rutas del AE


def test_the_ae_routes_need_its_key() -> None:
    client, *_ = _app()
    assert _ae(client, "GET", "", token="z" * 48).status_code == 401


def test_the_agent_reads_what_is_in_force_with_its_sections() -> None:
    client, *_ = _app()
    data = _ae(client, "GET", "").json()["data"]
    assert data["vigente"] == {"origen": "archivo", "version": KNOWLEDGE.version, "sha256": KNOWLEDGE.rendered_sha256}
    assert data["secciones"]["faq"] == RAW["faq"] and "cabecera" not in data["secciones"]
    assert data["estado"] == "origen=archivo,refresh=ok"


def test_the_preview_shows_the_diff_and_blocks_without_saving() -> None:
    client, db, *_ = _app()
    data = _ae(client, "POST", "/preview", {"cambios": _faq_change()}).json()["data"]
    assert data["ok"] is True and data["diff"]["agregadas"] == 1 and data["secciones"] == ["faq"]
    blocked = _ae(client, "POST", "/preview", {"cambios": _faq_change(" Hoy USD 97.")}).json()["data"]
    assert blocked["ok"] is False and blocked["blocks"][0]["code"] == "knowledge_amount_not_in_catalog"
    refused = _ae(client, "POST", "/preview", {"cambios": {"cabecera": {}}})
    assert refused.status_code == 422 and refused.json()["detail"] == "knowledge_change_invalid"
    assert db.calls == []


def test_saving_blocks_when_the_rules_block() -> None:
    client, db, *_ = _app()
    response = _propose(client, _faq_change(" Hoy USD 97."))
    assert response.status_code == 422 and response.json()["detail"] == "knowledge_change_blocked"
    assert db.calls == []


def test_saving_stores_the_whole_version_over_the_file_and_gives_the_link() -> None:
    client, db, *_ = _app()
    response = _propose(client)
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["approval_url"] == f"{ORIGIN}/flow-approval/k/{KNOWLEDGE_ID}" and data["status"] == "validada"
    [saved] = db.saved
    assert saved["p_base_origin"] == "archivo" and saved["p_base_knowledge_id"] is None
    assert saved["p_base_sha256"] == saved["p_file_sha256"] == KNOWLEDGE.rendered_sha256
    assert saved["p_change"] == {"cambios": _faq_change()} and saved["p_knowledge"]["faq"] == _faq_change()["faq"]
    assert saved["p_validator"]["git_sha"] == "abcdef0" and saved["p_requested_by_ref"] == ASKER["slack_user"]
    rebuilt = live_knowledge.build_knowledge(saved["p_knowledge"], file_version=KNOWLEDGE.version, ally_ref="att1")
    assert saved["p_rendered_sha256"] == rebuilt.rendered_sha256


def test_the_agent_has_no_route_to_publish_or_return_to_the_file() -> None:
    client, *_ = _app()
    paths = {route.path for route in client.app.routes}  # type: ignore[attr-defined]
    assert not any(path.startswith(PREFIX) and path.endswith(("/publish", "/return-to-file", "/discard"))
                   for path in paths)


# --------------------------------------------------------------- la pagina


def test_the_page_needs_a_sign_in() -> None:
    client, db, *_ = _app()
    _propose(client)
    assert client.get(PAGE, follow_redirects=False).status_code == 401
    assert db.rows[KNOWLEDGE_ID]["status"] == "validada"


def test_the_page_shows_the_purpose_and_the_diff_with_no_ids() -> None:
    client, db, _ = _signed()
    _propose(client)
    page = client.get(PAGE).text
    visible = _visible(page)
    assert "Suma una linea a la primera FAQ" in visible and "Aprobar y usar" in page
    assert "+ " in visible and "Te escribimos por aca." in visible
    assert KNOWLEDGE_ID not in visible and FINGERPRINT not in visible and DAN not in visible


def test_approving_serves_it_without_waiting_and_returning_goes_back_to_the_file() -> None:
    client, db, processor = _signed()
    _propose(client)
    assert _done(_publish(client)) == "publicada"
    source = processor.source
    assert source is not None
    assert source.current().origin == "vivo" and source.current().knowledge_id == KNOWLEDGE_ID
    assert "Te escribimos por aca." in source.current().rendered
    assert client.get("/ready").json()["live_knowledge"] == "origen=vivo:v1,refresh=ok"
    html = client.get(PAGE).text
    assert _done(_post(client, "return-to-file", csrf_token=_field(html, "csrf_token"))) == "archivo"
    assert source.current().origin == "archivo"


def test_with_a_segment_in_force_there_is_no_button_and_nothing_is_published() -> None:
    client, db, processor = _signed()
    _propose(client)
    assert processor.source is not None
    processor.source._segments_in_force = lambda: ("black-november-2026",)  # noqa: SLF001 - el catalogo, simulado
    page = client.get(PAGE).text
    assert "Aprobar y usar" not in page and "instrucción temporal" in _visible(page)
    response = _post(client, "publish", csrf_token=_field(page, "csrf_token"), fingerprint=FINGERPRINT)
    assert _done(response) == "knowledge_segments_in_force"
    assert "approver_publish_agent_knowledge_v1" not in db.calls


def test_a_version_armed_over_what_is_no_longer_in_force_is_not_published() -> None:
    client, db, processor = _signed()
    _propose(client)
    _propose(client, _faq_change(" Otra linea."))  # la segunda, sobre el mismo archivo
    assert _done(_publish(client)) == "publicada"
    other = f"/flow-approval/k/{OTHER_ID}"
    page = client.get(other).text
    assert "Aprobar y usar" not in page and "Pedile al agente que la arme de nuevo" in _visible(page)
    response = _post(client, "publish", other, csrf_token=_field(page, "csrf_token"), fingerprint=FINGERPRINT)
    assert _done(response) == "agent_knowledge_base_changed"
    assert processor.source is not None and processor.source.current().knowledge_id == KNOWLEDGE_ID


def test_a_tampered_row_is_not_published() -> None:
    client, db, _ = _signed()
    _propose(client)
    db.rows[KNOWLEDGE_ID]["knowledge"]["faq"][0]["respuesta"] += " Cambio por fuera."  # la base, adulterada
    page = client.get(PAGE).text
    assert "Aprobar y usar" not in page
    response = _post(client, "publish", csrf_token=_field(page, "csrf_token"), fingerprint=FINGERPRINT)
    assert _done(response) == "agent_knowledge_render_changed"


def test_discard_only_what_was_not_published() -> None:
    client, db, _ = _signed()
    _propose(client)
    html = client.get(PAGE).text
    assert _done(_post(client, "discard", csrf_token=_field(html, "csrf_token"))) == "descartada"
    assert db.rows[KNOWLEDGE_ID]["status"] == "descartada"


def test_after_approving_the_next_change_is_armed_over_the_live_version() -> None:
    client, db, _ = _signed()
    _propose(client)
    _done(_publish(client))
    data = _ae(client, "GET", "").json()["data"]
    assert data["vigente"]["origen"] == "vivo" and data["vigente"]["knowledge_id"] == KNOWLEDGE_ID
    response = _propose(client, {"faq": data["secciones"]["faq"] + [
        {"pregunta": "¿Hay app?", "respuesta": "No, es por la web.", "fuente": "test", "vigencia": "2026-11-26"}]})
    assert response.status_code == 201, response.text
    assert db.saved[-1]["p_base_origin"] == "vivo" and db.saved[-1]["p_base_knowledge_id"] == KNOWLEDGE_ID


def test_the_segments_in_force_are_those_of_the_catalog_of_the_turn_that_have_not_ended() -> None:
    # D-55.4 en el cableado: vigente o por empezar cuenta; vencido no.
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from bridge import app as app_module

    now = datetime.now(UTC)
    catalog = SimpleNamespace(segments=[
        SimpleNamespace(key="vigente", knowledge=SimpleNamespace(valid_until=now + timedelta(days=2))),
        SimpleNamespace(key="vencido", knowledge=SimpleNamespace(valid_until=now - timedelta(days=1))),
    ])
    processor = _Processor()
    source = app_module._live_knowledge_setup(_settings(**ENABLED), Base(Db()), processor, catalog)
    assert source is not None and processor.source is source
    assert source.segments_in_force() == ("vigente",)
    assert app_module._live_knowledge_setup(_settings(**ENABLED), Base(Db()), processor, None).segments_in_force() == ()
    assert app_module._live_knowledge_setup(_settings(), Base(Db()), _Processor(), catalog) is None
