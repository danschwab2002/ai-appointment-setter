"""Las rutas del AE y la pagina de una campana (agent-campaigns-v1, E-355).

Los dobles de ``test_flow_approval_page`` (la app con el cliente real de la base,
PostgREST y Slack emulados). El emulador sigue las reglas de 20261108005000 que le
importan a estas rutas; lo que la base hace de verdad lo prueba PGlite
(``validate_agent_campaigns.mjs``). La campana es ficticia.
"""

from __future__ import annotations

import base64
import copy
import json
import re
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from test_agent_campaigns import CAMPAIGN, FINGERPRINT, LABEL, MACRO, _draft
from test_flow_approval_page import ATT1 as ATT1_DIR
from test_flow_approval_page import (
    BRAYAN,
    CLIENT_ID,
    CLIENT_SECRET,
    DAN,
    DB_KEY,
    ORIGIN,
    OUTSIDER,
    FakeChatwoot,
    PostgrestEmulator,
    SlackEmulator,
    _Clock,
    _NoSupabase,
    _settings,
    _sign_in,
)

from bridge import agent_campaign_api, agent_campaigns, automation_expert_chatwoot, flow_approval
from bridge.app import create_app
from bridge.commercial_knowledge import CommercialKnowledge
from bridge.segment_knowledge import SegmentCatalog

PREFIX = "/internal/automation-expert/v1"
TOKEN = "c" * 48
AE_KEY_CLAIMS = {"role": "automation_expert_change"}
PAGE = f"/flow-approval/c/{CAMPAIGN}"
FORM = {"Content-Type": "application/x-www-form-urlencoded", "Origin": ORIGIN}
ASKER = {"slack_user": "U01ASKER00"}
KNOWLEDGE = CommercialKnowledge.from_toml_file(ATT1_DIR / "conocimiento" / "knowledge-v1.toml", require_approved=False)


def _jwt(claims: dict) -> str:
    part = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{part({'alg': 'RS256', 'kid': 'k'})}.{part(claims)}.firma"


AE_KEY = _jwt(AE_KEY_CLAIMS)
ENABLED = {
    "agent_campaigns_enabled": True, "agent_segments_enabled": True, "campaign_inbound_entry_enabled": True,
    "commercial_knowledge": KNOWLEDGE,
    "agent_segment_catalog": SegmentCatalog((), len(KNOWLEDGE.render().encode("utf-8"))),
    "automation_expert_change_enabled": True, "automation_expert_change_token": TOKEN,
    "automation_expert_change_db_key": AE_KEY,
    "automation_expert_read_enabled": True, "automation_expert_read_token": "r" * 48,
    "chatwoot_account_id": 2, "chatwoot_inbox_id": 11,
}


class Db(PostgrestEmulator):
    """Las funciones de la campana, con las reglas de la migracion que le importan a la pagina."""

    def __init__(self) -> None:
        super().__init__()
        self.row: dict[str, Any] | None = None
        self.campaign_calls: list[str] = []
        self.live: dict[str, Any] | None = None

    def set_validated(self, check_sha: str | None = None, *, report: dict | None = None) -> None:
        draft = agent_campaigns.parse_campaign(_draft())
        self.row = {"campaign_id": CAMPAIGN, "status": "validada", "effective_status": "validada", "revision": 1,
                    "campaign_key": draft["clave"], "segment_version": 1, "label": LABEL, "draft": draft,
                    "purpose": "Black November", "valid_from": draft["desde"], "valid_until": draft["hasta"],
                    "validation": {"preview": {"render": "# Segmento: Black November\nPrecio USD 37",
                                               "wa_me": "https://wa.me/5215512345678?text=Black%20November"},
                                   "result": {"blocks": [], "warnings": []}},
                    "fingerprint": FINGERPRINT, "validated_at": "2026-11-08T12:00:00Z",
                    "expires_at": "2026-11-09T12:00:00Z", "check_sha": check_sha, "check_report": report,
                    "checked_at": None, "approved_at": None, "approved_by_kind": None, "approved_by_ref": None,
                    "paused_at": None, "paused_by_ref": None, "closed_at": None, "closed_by_kind": None,
                    "created_at": "2026-11-08T11:59:00Z", "events": []}

    def handler(self, request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[1]
        if "agent_campaign" not in name:
            return super().handler(request)
        body = json.loads(request.content)
        self.campaign_calls.append(name)
        if name.startswith("approver_"):
            session = self._session(body)
            if session is None:
                return self._raise("28000", "flow_approval_session_invalid", 403)
            if body["p_campaign_id"] != CAMPAIGN or self.row is None:
                return self._raise("P0002", "agent_campaign_not_found", 500)
            return getattr(self, "_" + name)(body, session["user"])
        return getattr(self, "_" + name)(body)

    def _approver_get_agent_campaign_v1(self, _: dict, __: str) -> httpx.Response:
        return httpx.Response(200, json=copy.deepcopy(self.row))

    def _approver_publish_agent_campaign_v1(self, body: dict, user: str) -> httpx.Response:
        row = self.row
        assert row is not None
        if body["p_fingerprint"] != row["fingerprint"]:
            return self._raise("22023", "agent_campaign_fingerprint_mismatch")
        if row["status"] == "publicada":
            return httpx.Response(200, json={"ok": True, "replayed": True, "status": "publicada"})
        if row["status"] != "validada":
            return httpx.Response(200, json={"ok": False, "reason_code": "agent_campaign_not_validated"})
        if row["check_sha"] is None:
            return httpx.Response(200, json={"ok": False, "reason_code": "agent_campaign_check_missing"})
        if body["p_check_sha"] != row["check_sha"]:
            return httpx.Response(200, json={"ok": False, "reason_code": "agent_campaign_check_stale"})
        if self.live is not None:
            return httpx.Response(200, json={"ok": False, "reason_code": "agent_campaign_live_exists"})
        row.update(status="publicada", effective_status="publicada", approved_at="2026-11-08T13:00:00Z",
                   approved_by_kind="slack", approved_by_ref=user)
        self.live = {"campaign_id": CAMPAIGN, "status": "publicada", "campaign_key": row["campaign_key"],
                     "segment_version": 1, "label": LABEL, "label_macro_id": MACRO, "draft": row["draft"],
                     "fingerprint": FINGERPRINT, "check_sha": row["check_sha"], "approved_at": row["approved_at"]}
        return httpx.Response(200, json={"ok": True, "replayed": False, "status": "publicada"})

    def _approver_pause_agent_campaign_v1(self, _: dict, user: str) -> httpx.Response:
        row = self.row
        assert row is not None
        if row["status"] != "publicada":
            return httpx.Response(200, json={"ok": False, "reason_code": "agent_campaign_not_live"})
        row.update(status="pausada", effective_status="pausada", paused_by_ref=user)
        self.live = None
        return httpx.Response(200, json={"ok": True, "replayed": False, "status": "pausada"})

    def _approver_resume_agent_campaign_v1(self, _: dict, __: str) -> httpx.Response:
        row = self.row
        assert row is not None
        if row["status"] != "pausada":
            return httpx.Response(200, json={"ok": False, "reason_code": "agent_campaign_not_paused"})
        row.update(status="publicada", effective_status="publicada")
        self.live = {"campaign_id": CAMPAIGN, "status": "publicada", "campaign_key": row["campaign_key"],
                     "segment_version": 1, "label": LABEL, "label_macro_id": MACRO, "draft": row["draft"],
                     "fingerprint": FINGERPRINT, "check_sha": row["check_sha"], "approved_at": row["approved_at"]}
        return httpx.Response(200, json={"ok": True, "replayed": False, "status": "publicada"})

    def _approver_discard_agent_campaign_v1(self, _: dict, __: str) -> httpx.Response:
        row = self.row
        assert row is not None
        if row["status"] not in ("borrador", "validada"):
            return httpx.Response(200, json={"ok": False, "reason_code": "agent_campaign_final"})
        row.update(status="descartada", effective_status="descartada")
        return httpx.Response(200, json={"ok": True, "replayed": False})

    # El rol del agente.
    def _save_agent_campaign_draft_v1(self, body: dict) -> httpx.Response:
        assert request_key(body) == {"tenant_ref": "lancemos", "ally_ref": "att1"}
        draft = copy.deepcopy(body["p_draft"])
        if self.row is None:
            self.set_validated()
            assert self.row is not None
            self.row.update(status="borrador", effective_status="borrador", fingerprint=None)
        self.row["draft"] = draft
        self.row["revision"] += 0 if body["p_campaign_id"] is None else 1
        return httpx.Response(200, json={"ok": True, "campaign_id": CAMPAIGN, "revision": self.row["revision"],
                                         "segment_version": 1, "status": "borrador"})

    def _record_agent_campaign_validation_v1(self, body: dict) -> httpx.Response:
        assert self.row is not None
        self.row["last_result"] = body["p_result"]
        if body["p_result"]["blocks"]:
            return httpx.Response(200, json={"ok": True, "status": "borrador", "blocks": len(body["p_result"]["blocks"])})
        self.row.update(status="validada", effective_status="validada", fingerprint=FINGERPRINT,
                        validation={"validator": body["p_validator"], "result": body["p_result"],
                                    "preview": body["p_preview"]})
        return httpx.Response(200, json={"ok": True, "status": "validada", "fingerprint": FINGERPRINT,
                                         "expires_at": "2026-11-09T12:00:00Z"})

    def _get_agent_campaign_v1(self, _: dict) -> httpx.Response:
        return httpx.Response(200, json=copy.deepcopy(self.row))

    def _list_agent_campaigns_v1(self, _: dict) -> httpx.Response:
        return httpx.Response(200, json={"campaigns": [{"campaign_id": CAMPAIGN}], "open": 1, "open_limit": 5,
                                         "database_at": "2026-11-08T13:00:00Z"})


def request_key(body: dict) -> dict:
    return body["p_instance"]


class Chatwoot(FakeChatwoot):
    def __init__(self, *, actions: list[dict] | None = None, macro_error: Exception | None = None) -> None:
        super().__init__()
        self.actions = actions if actions is not None else [{"action_name": "add_label", "action_params": [LABEL]}]
        self.macro_error = macro_error

    async def get_inbox(self, *, inbox_id: int) -> dict:
        return {**await super().get_inbox(inbox_id=inbox_id), "phone_number": "+5215512345678"}

    async def get_macro_actions(self, *, macro_id: int) -> list[dict]:
        if self.macro_error is not None:
            raise self.macro_error
        return self.actions

    async def get_conversation_labels(self, *, conversation_id: int) -> list[str]:
        return []


class Base(_NoSupabase):
    """Solo lo que usa el bridge con el flag: la lectura del catalogo vivo y las respuestas a la pregunta."""

    def __init__(self, db: Db, *, down: bool = False) -> None:
        self.__dict__["db"] = db
        self.__dict__["down"] = down

    async def resolve_commercial_ally_runtime_binding(self, config: object) -> object:
        return config

    async def get_human_handoff_projection_status(self) -> object:
        return SimpleNamespace(pending_count=0, retryable_count=0, delivery_unknown_count=0,
                               conflict_count=0, dead_letter_count=0)

    async def get_live_agent_campaigns(self, instance: dict[str, str]) -> dict[str, Any]:
        if self.down:
            raise OSError("caida")
        return {"campaigns": [self.db.live] if self.db.live else []}

    async def get_segment_entry_answers(self, **_: Any) -> dict[str, str]:
        return {}

    async def record_segment_entry_answer(self, **_: Any) -> str:
        return "recorded"


@pytest.fixture(autouse=True)
def _image_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SETTER_VERSION", "v1.33.0")
    monkeypatch.setenv("GIT_SHA", "abcdef0")


class _Processor:
    accepts_campaign_answers = True


def _app(db: Db | None = None, *, chatwoot: Chatwoot | None = None, **overrides: Any) -> tuple[TestClient, Db, SlackEmulator, _Clock]:
    db = db or Db()
    slack = SlackEmulator()
    clock = _Clock()
    approval = flow_approval.ApprovalStore(base_url="https://att1-rest.internal", db_key=DB_KEY,
                                           transport=httpx.MockTransport(db.handler))
    oidc = flow_approval.SlackIdTokenClient(client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                                            transport=httpx.MockTransport(slack.handler), clock=lambda: clock.now)
    campaign_store = agent_campaign_api.CampaignStore(base_url="https://att1-rest.internal", db_key=AE_KEY,
                                                      transport=httpx.MockTransport(_as_approver(db)))
    settings = _settings(**{**ENABLED, **overrides})
    app = create_app(settings, shadow_processor=_Processor(), supabase_client=Base(db),  # type: ignore[arg-type]
                     chatwoot_client=chatwoot or Chatwoot(),  # type: ignore[arg-type]
                     flow_approval_store=approval, flow_approval_oidc=oidc, agent_campaign_store=campaign_store)
    client = TestClient(app, base_url=ORIGIN)
    client.__enter__()
    return client, db, slack, clock


def _as_approver(db: Db) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {AE_KEY}"
        return db.handler(request)

    return handler


def _signed(db: Db | None = None, *, user: str = DAN, **overrides: Any) -> tuple[TestClient, Db]:
    client, db, slack, clock = _app(db, **overrides)
    _sign_in(client, slack, clock, user=user)
    return client, db


def _visible(page: str) -> str:
    page = re.sub(r"<style.*?</style>", " ", page, flags=re.S)
    page = re.sub(r"<details.*?</details>", " ", page, flags=re.S)
    return re.sub(r"<[^>]+>", " ", page)


def _field(page: str, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]+)"', page)
    assert match is not None, name
    return match.group(1)


def _page(client: TestClient) -> str:
    response = client.get(PAGE)
    assert response.status_code == 200, response.text
    return response.text


def _post(client: TestClient, action: str, **fields: str) -> httpx.Response:
    return client.post(f"{PAGE}/{action}", content=urlencode(fields), headers=FORM, follow_redirects=False)


def _done(response: httpx.Response) -> str:
    assert response.status_code == 303, response.text
    query = {key: values[0] for key, values in parse_qs(urlsplit(response.headers["location"]).query).items()}
    return query["hecho"]


def _publish_fields(page: str) -> dict[str, str]:
    return {"csrf_token": _field(page, "csrf_token"), "fingerprint": _field(page, "fingerprint")}


def _sha_for(db: Db, client: TestClient) -> str:
    assert db.row is not None
    context = agent_campaigns.CampaignContext(manifest=_settings().instance_manifest, base=KNOWLEDGE,
                                              manifest_catalog=ENABLED["agent_segment_catalog"])
    return agent_campaigns.expected_check_sha(db.row["draft"], version=1, context=context)


def _validated(client_db: tuple[TestClient, Db], *, clean: bool = True) -> None:
    client, db = client_db
    db.set_validated()
    if clean:
        db.row["check_sha"] = _sha_for(db, client)
        db.row["check_report"] = {"veredicto": "limpio", "abiertos": 0, "items": []}


# ------------------------------------------------------------------ las rutas del AE


def _ae(client: TestClient, method: str, path: str, body: dict | None = None, *, token: str = TOKEN) -> httpx.Response:
    return client.request(method, f"{PREFIX}/campaigns{path}", json=body,
                          headers={"Authorization": f"Bearer {token}"})


def test_the_ae_routes_need_its_key() -> None:
    client, *_ = _app()
    assert _ae(client, "POST", "/preview", {"campaign": _draft()}, token="z" * 48).status_code == 401


def test_the_preview_validates_and_renders_without_saving() -> None:
    client, db, *_ = _app()
    response = _ae(client, "POST", "/preview", {"campaign": _draft()})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["ok"] is True and "Precio en esta campaña: USD 37." in data["preview"]["render"]
    assert data["preview"]["wa_me"] == "https://wa.me/5215512345678?text=Black%20November"
    assert db.campaign_calls == []


def test_the_preview_reports_blocks_without_failing_and_refuses_what_does_not_parse() -> None:
    client, *_ = _app()
    data = _ae(client, "POST", "/preview", {"campaign": _draft(etiqueta="cliente")}).json()["data"]
    assert data["ok"] is False and data["blocks"][0]["code"] == "campaign_label_reserved"
    refused = _ae(client, "POST", "/preview", {"campaign": _draft(desde="sin zona")})
    assert refused.status_code == 422 and refused.json()["detail"] == "campaign_invalid"


def test_saving_blocks_when_the_rules_block() -> None:
    client, db, *_ = _app()
    response = _ae(client, "POST", "", {"campaign": _draft(etiqueta="cliente"), "purpose": "x", "requested_by": ASKER})
    assert response.status_code == 422 and response.json()["detail"] == "campaign_draft_blocked"
    assert db.campaign_calls == []


def test_saving_and_validating_freezes_it_and_checks_the_macro() -> None:
    client, db, *_ = _app()
    saved = _ae(client, "POST", "", {"campaign": _draft(), "purpose": "Black November", "requested_by": ASKER})
    assert saved.status_code == 201, saved.text
    assert saved.json()["data"]["campaign_id"] == CAMPAIGN
    validated = _ae(client, "POST", f"/{CAMPAIGN}/validate", {"revision": 1, "requested_by": ASKER})
    assert validated.status_code == 200, validated.text
    assert validated.json()["data"]["status"] == "validada"
    assert validated.json()["data"]["approval_url"] == f"{ORIGIN}/flow-approval/c/{CAMPAIGN}"
    assert "record_agent_campaign_validation_v1" in db.campaign_calls


@pytest.mark.parametrize(("chatwoot", "code"), [
    (Chatwoot(actions=[{"action_name": "add_label", "action_params": [LABEL]},
                       {"action_name": "send_message", "action_params": ["hola"]}]),
     "campaign_macro_does_more_than_add_the_label"),
    (Chatwoot(macro_error=OSError("no contesto")), "campaign_macro_unreadable"),
])
def test_validating_blocks_with_a_macro_that_does_more_or_cannot_be_read(chatwoot: Chatwoot, code: str) -> None:
    client, db, *_ = _app(chatwoot=chatwoot)
    _ae(client, "POST", "", {"campaign": _draft(), "purpose": "x", "requested_by": ASKER})
    validated = _ae(client, "POST", f"/{CAMPAIGN}/validate", {"revision": 1, "requested_by": ASKER})
    assert validated.json()["data"]["status"] == "borrador"
    assert [block["code"] for block in validated.json()["data"]["blocks"]] == [code]


def test_the_agent_has_no_route_to_publish_or_pause() -> None:
    client, *_ = _app()
    paths = {route.path for route in client.app.routes}  # type: ignore[attr-defined]
    assert not any(path.startswith(f"{PREFIX}/campaigns") and path.endswith(("/publish", "/pause", "/resume"))
                   for path in paths)


# -------------------------------------------------------------------- la pagina


def test_the_page_needs_a_sign_in_and_comes_back_to_the_campaign() -> None:
    client, db, slack, clock = _app()
    db.set_validated()
    response = client.get(PAGE, follow_redirects=False)
    assert response.status_code == 401 and f"?c={CAMPAIGN}" in response.text


def test_without_a_session_nothing_is_published() -> None:
    client, db, slack, clock = _app()
    db.set_validated(check_sha="a" * 64)
    response = _post(client, "publish", csrf_token="x" * 64, fingerprint=FINGERPRINT)
    assert response.status_code == 401
    assert db.row["status"] == "validada" and db.live is None


def test_someone_out_of_the_list_does_not_get_in() -> None:
    client, db, slack, clock = _app()
    _sign_in(client, slack, clock, user=OUTSIDER)
    db.set_validated()
    assert client.get(PAGE, follow_redirects=False).status_code == 401


def test_the_page_is_in_business_words_with_no_ids() -> None:
    client_db = _signed()
    client, db = client_db
    _validated(client_db)
    page = _page(client)
    visible = _visible(page)
    assert "Black November" in visible and "Aprobar y publicar" in page
    assert "Quien empieza la conversación con «Black November»" in visible
    assert "wa.me/5215512345678" in visible
    assert "Confirmá que exista en Hotmart" in visible
    assert CAMPAIGN not in visible and FINGERPRINT not in visible and DAN not in visible


def test_without_a_clean_check_there_is_no_button_and_the_base_does_not_publish() -> None:
    client_db = _signed()
    client, db = client_db
    _validated(client_db, clean=False)
    page = _page(client)
    assert "Aprobar y publicar" not in page and "Falta el control de calidad" in page
    assert _done(_post(client, "publish", csrf_token=_field(page, "csrf_token"), fingerprint=FINGERPRINT)) \
        == "agent_campaign_check_missing"
    assert db.row["status"] == "validada" and db.live is None


def test_with_open_questions_the_page_shows_them_and_no_button() -> None:
    client_db = _signed()
    client, db = client_db
    _validated(client_db, clean=False)
    db.row["check_report"] = {"veredicto": "abiertos", "abiertos": 1, "items": [{"pregunta": "¿Sirve en Colombia?"}]}
    page = _page(client)
    assert "Aprobar y publicar" not in page and "¿Sirve en Colombia?" in page


def test_a_check_from_before_the_base_changed_is_not_published() -> None:
    client_db = _signed()
    client, db = client_db
    _validated(client_db)
    db.row["check_sha"] = "b" * 64
    page = _page(client)
    assert "Aprobar y publicar" not in page and "conocimiento general" in _visible(page)
    assert _done(_post(client, "publish", csrf_token=_field(page, "csrf_token"), fingerprint=FINGERPRINT)) \
        == "agent_campaign_check_stale"
    assert db.live is None


def test_approving_publishes_and_the_catalog_serves_it_without_waiting() -> None:
    client_db = _signed()
    client, db = client_db
    _validated(client_db)
    page = _page(client)
    assert _done(_post(client, "publish", **_publish_fields(page))) == "publicada"
    assert db.live is not None
    assert "Andando" in _page(client)


def test_two_live_campaigns_are_refused() -> None:
    client_db = _signed()
    client, db = client_db
    _validated(client_db)
    db.live = {"campaign_id": "otra"}
    page = _page(client)
    assert _done(_post(client, "publish", **_publish_fields(page))) == "agent_campaign_live_exists"
    assert db.row["status"] == "validada"


def test_another_origin_or_a_bad_csrf_do_not_publish() -> None:
    client_db = _signed()
    client, db = client_db
    _validated(client_db)
    page = _page(client)
    fields = _publish_fields(page)
    response = client.post(f"{PAGE}/publish", content=urlencode(fields),
                           headers={**FORM, "Origin": "https://otro.example.org"}, follow_redirects=False)
    assert response.status_code == 403
    assert _post(client, "publish", csrf_token="0" * 64, fingerprint=FINGERPRINT).status_code == 403
    assert db.live is None


def test_pause_and_resume_change_what_is_served() -> None:
    client_db = _signed()
    client, db = client_db
    _validated(client_db)
    page = _page(client)
    _post(client, "publish", **_publish_fields(page))
    page = _page(client)
    assert "Pausar" in page
    assert _done(_post(client, "pause", csrf_token=_field(page, "csrf_token"))) == "pausada"
    assert db.live is None
    page = _page(client)
    assert _done(_post(client, "resume", csrf_token=_field(page, "csrf_token"))) == "reanudada"
    assert db.live is not None


def test_discard_only_what_was_not_published() -> None:
    client_db = _signed()
    client, db = client_db
    _validated(client_db)
    page = _page(client)
    assert _done(_post(client, "discard", csrf_token=_field(page, "csrf_token"), motivo="no va")) == "descartada"
    assert db.row["status"] == "descartada"


def test_a_published_campaign_the_bridge_cannot_serve_says_so() -> None:
    db = Db()
    db.set_validated()
    db.row.update(status="publicada", effective_status="publicada", check_sha="b" * 64,
                  check_report={"veredicto": "limpio", "abiertos": 0, "items": []})
    db.live = {"campaign_id": CAMPAIGN, "status": "publicada", "campaign_key": db.row["campaign_key"],
               "segment_version": 1, "label": LABEL, "label_macro_id": MACRO, "draft": db.row["draft"],
               "fingerprint": FINGERPRINT, "check_sha": "b" * 64, "approved_at": "2026-11-08T13:00:00Z"}
    client, db = _signed(db)
    assert "NO la está usando" in _visible(_page(client))


# ------------------------------------------------- el cableado en create_app (E-355)


def _servable(macro: int = MACRO) -> Db:
    """Una campana publicada que el bridge puede servir: vigente hasta 2099 y con el chequeo_sha de verdad."""
    db = Db()
    draft = agent_campaigns.parse_campaign(_draft(desde="2026-01-01T00:00:00-06:00", hasta="2099-12-01T00:00:00-06:00",
                                                  macro_etiqueta=macro))
    context = agent_campaigns.CampaignContext(manifest=_settings().instance_manifest, base=KNOWLEDGE,
                                              manifest_catalog=ENABLED["agent_segment_catalog"])
    db.live = {"campaign_id": CAMPAIGN, "status": "publicada", "campaign_key": draft["clave"], "segment_version": 1,
               "label": LABEL, "label_macro_id": macro, "draft": draft, "fingerprint": FINGERPRINT,
               "check_sha": agent_campaigns.expected_check_sha(draft, version=1, context=context),
               "approved_at": "2026-11-08T13:00:00Z"}
    return db


@pytest.fixture
def live_catalogs(monkeypatch: pytest.MonkeyPatch) -> list[agent_campaigns.LiveSegmentCatalog]:
    made: list[agent_campaigns.LiveSegmentCatalog] = []

    class Recorded(agent_campaigns.LiveSegmentCatalog):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            made.append(self)

    monkeypatch.setattr(agent_campaigns, "LiveSegmentCatalog", Recorded)
    return made


def _ready(client: TestClient) -> dict[str, Any]:
    return client.get("/ready").json()


def test_the_turn_reads_the_live_catalog_not_the_manifest_one() -> None:
    client, _, _, _ = _app(_servable())
    ready = _ready(client)
    assert ready["agent_campaigns"] == "served=1,not_served=0,refresh=ok"
    assert "black-november-2026:v1:" in ready["agent_segments"]


def test_the_entry_is_bound_to_the_catalog(live_catalogs: list[agent_campaigns.LiveSegmentCatalog]) -> None:
    client, _, _, _ = _app(_servable())
    [live] = live_catalogs
    entry = live._entry
    assert entry is not None and [segment.key for segment in entry.segments] == ["black-november-2026"]
    client.__exit__(None, None, None)


@pytest.mark.parametrize("setting", ["chatwoot_pause_macro_id", "chatwoot_resume_macro_id", "chatwoot_opt_out_macro_id",
                                     "chatwoot_client_label_macro_id"])
def test_a_macro_of_the_control_of_the_bridge_is_not_served(setting: str) -> None:
    client, _, _, _ = _app(_servable(), **{setting: MACRO})
    assert _ready(client)["agent_campaigns"] == "served=0,not_served=1,refresh=ok"


def test_the_refresh_stops_with_the_bridge(live_catalogs: list[agent_campaigns.LiveSegmentCatalog]) -> None:
    client, _, _, _ = _app(_servable())
    [live] = live_catalogs
    task = live._task
    assert task is not None and not task.done()
    client.__exit__(None, None, None)
    assert live._task is None and task.done()


def test_creating_a_macro_comes_with_the_campaigns(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[dict[str, Any]] = []
    real = automation_expert_chatwoot.ScopedChatwoot

    def scoped(**kwargs: Any) -> Any:
        built.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(automation_expert_chatwoot, "ScopedChatwoot", scoped)
    client, _, _, _ = _app(automation_expert_chatwoot_enabled=True, chatwoot_control_api_access_token="k" * 24)
    auth = {"Authorization": f"Bearer {TOKEN}"}
    response = client.post(f"{PREFIX}/chatwoot/macros", json={"label": "cliente", "requested_by": ASKER}, headers=auth)
    assert response.status_code == 422 and response.json()["detail"] == "chatwoot_label_reserved"
    # Ni la ruta sola alcanza: la lista cerrada tambien lo tiene que dejar pasar.
    assert built[0]["create_macros"] is True


def test_every_text_of_the_model_is_escaped() -> None:
    client_db = _signed()
    client, db = client_db
    _validated(client_db, clean=False)
    db.row["draft"]["nombre"] = "<script>x</script>"
    db.row["check_report"] = {"veredicto": "abiertos", "items": [{"pregunta": "<img src=x>"}]}
    page = _page(client)
    assert "<script>x</script>" not in page and "<img src=x>" not in page
