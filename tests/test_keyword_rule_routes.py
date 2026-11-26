"""Las rutas del AE y la pagina de una regla de palabra clave (Z1 3.9, ``keyword-rules-v1``).

Los dobles de ``test_flow_approval_page`` y ``test_agent_campaign_routes`` (la app con el
cliente real de la base, PostgREST y Slack emulados). El emulador sigue las reglas de
20261126400000 que le importan a estas rutas; lo que la base hace de verdad lo prueba
PGlite (``validate_keyword_rules.mjs``). El macro tiene la forma del GET real de un macro
de ATT1 (``chatwoot_macros_account_2_20261016.json``) con la accion ``add_label`` de la
etiqueta de la regla. La regla es ficticia.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from test_agent_campaign_routes import AE_KEY, ASKER, ENABLED, FORM, PREFIX, TOKEN, Base, Db, _Processor
from test_flow_approval_page import DAN, DB_KEY, ORIGIN, OUTSIDER, CLIENT_ID, CLIENT_SECRET, FakeChatwoot
from test_flow_approval_page import SlackEmulator, _Clock, _settings, _sign_in

from bridge import flow_approval, keyword_rule_api, keyword_rules
from bridge.app import create_app

FIXTURES = Path(__file__).parent / "fixtures"
MACRO = json.loads((FIXTURES / "chatwoot_macros_account_2_20261016.json").read_text(encoding="utf-8"))
RULE = "7a2b3c4d-2222-4b9f-8d2e-000000000001"
PAGE = f"/flow-approval/w/{RULE}"
FINGERPRINT = "f" * 64
LABEL = "soporte"
MACRO_ID = 77
KEYWORDS = {**ENABLED, "keyword_rules_enabled": True, "agent_campaigns_enabled": False,
            "campaign_inbound_entry_enabled": False, "agent_segments_enabled": False,
            "agent_segment_catalog": None}


def _rule(**changes: Any) -> dict[str, Any]:
    raw = {"clave": "soporte", "nombre": "Link de soporte", "palabra_clave": "Hola, necesito soporte",
           "etiqueta": LABEL, "macro_etiqueta": MACRO_ID}
    raw.update(changes)
    return raw


class RuleDb(Db):
    """Las funciones de la regla, con las reglas de la migracion que le importan a la pagina."""

    def __init__(self) -> None:
        super().__init__()
        self.rule_row: dict[str, Any] | None = None
        self.rule_calls: list[str] = []
        self.live_rules: list[dict[str, Any]] = []

    def set_draft(self, **changes: Any) -> None:
        rule = keyword_rules.parse_rule(_rule(**changes))
        self.rule_row = {"rule_id": RULE, "status": "borrador", "revision": 1, "rule_key": rule["clave"],
                         "keyword": rule["palabra_normalizada"], "label": rule["etiqueta"], "rule": rule,
                         "purpose": "El link de soporte", "fingerprint": FINGERPRINT, "approved_at": None,
                         "paused_at": None, "closed_at": None, "created_at": "2026-11-26T12:00:00Z", "events": [],
                         "approved_by_ref": None, "paused_by_ref": None}

    def _live_row(self) -> dict[str, Any]:
        assert self.rule_row is not None
        return {"rule_id": RULE, "rule_key": self.rule_row["rule_key"], "revision": 1,
                "rule": copy.deepcopy(self.rule_row["rule"]), "fingerprint": FINGERPRINT,
                "approved_at": "2026-11-26T13:00:00Z"}

    def handler(self, request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[1]
        if "keyword_rule" not in name:
            return super().handler(request)
        body = json.loads(request.content)
        self.rule_calls.append(name)
        if name.startswith("approver_"):
            session = self._session(body)
            if session is None:
                return self._raise("28000", "flow_approval_session_invalid", 403)
            if body["p_rule_id"] != RULE or self.rule_row is None:
                return self._raise("P0002", "keyword_rule_not_found", 500)
            return getattr(self, "_" + name)(body, session["user"])
        return getattr(self, "_" + name)(body)

    def _approver_get_keyword_rule_v1(self, _: dict, __: str) -> httpx.Response:
        return httpx.Response(200, json=copy.deepcopy(self.rule_row))

    def _approver_publish_keyword_rule_v1(self, body: dict, user: str) -> httpx.Response:
        row = self.rule_row
        assert row is not None
        if body["p_fingerprint"] != row["fingerprint"]:
            return self._raise("22023", "keyword_rule_fingerprint_mismatch")
        if row["status"] != "borrador":
            return httpx.Response(200, json={"ok": True, "replayed": True, "status": row["status"]})
        row.update(status="publicada", approved_at="2026-11-26T13:00:00Z", approved_by_ref=user)
        self.live_rules = [self._live_row()]
        return httpx.Response(200, json={"ok": True, "replayed": False, "status": "publicada"})

    def _approver_pause_keyword_rule_v1(self, _: dict, user: str) -> httpx.Response:
        row = self.rule_row
        assert row is not None
        if row["status"] != "publicada":
            return httpx.Response(200, json={"ok": False, "reason_code": "keyword_rule_not_live"})
        row.update(status="pausada", paused_by_ref=user)
        self.live_rules = []
        return httpx.Response(200, json={"ok": True, "replayed": False})

    def _approver_resume_keyword_rule_v1(self, _: dict, __: str) -> httpx.Response:
        row = self.rule_row
        assert row is not None
        if row["status"] != "pausada":
            return httpx.Response(200, json={"ok": False, "reason_code": "keyword_rule_not_paused"})
        row.update(status="publicada")
        self.live_rules = [self._live_row()]
        return httpx.Response(200, json={"ok": True, "replayed": False})

    def _approver_discard_keyword_rule_v1(self, _: dict, __: str) -> httpx.Response:
        row = self.rule_row
        assert row is not None
        if row["status"] not in ("borrador", "pausada"):
            return httpx.Response(200, json={"ok": False, "reason_code": "keyword_rule_live_pause_first"})
        row.update(status="descartada")
        return httpx.Response(200, json={"ok": True, "replayed": False})

    # El rol del agente.
    def _save_keyword_rule_draft_v1(self, body: dict) -> httpx.Response:
        assert body["p_instance"] == {"tenant_ref": "lancemos", "ally_ref": "att1"}
        assert body["p_rule"]["palabra_normalizada"] == "hola necesito soporte"
        self.set_draft()
        return httpx.Response(200, json={"ok": True, "rule_id": RULE, "revision": 1, "status": "borrador",
                                         "fingerprint": FINGERPRINT})

    def _get_keyword_rule_v1(self, _: dict) -> httpx.Response:
        return httpx.Response(200, json=copy.deepcopy(self.rule_row))

    def _list_keyword_rules_v1(self, _: dict) -> httpx.Response:
        rows = [{"rule_id": RULE, "status": self.rule_row["status"]}] if self.rule_row else []
        return httpx.Response(200, json={"rules": rows, "database_at": "2026-11-26T13:00:00Z"})


class Chatwoot(FakeChatwoot):
    def __init__(self, *, actions: list[dict] | None = None, macro_error: Exception | None = None) -> None:
        super().__init__()
        if actions is None:
            actions = copy.deepcopy(MACRO["get_macro_4"]["body"]["payload"]["actions"])
            actions[0]["action_params"] = [LABEL]
        self.actions = actions
        self.macro_error = macro_error

    async def get_inbox(self, *, inbox_id: int) -> dict:
        return {**await super().get_inbox(inbox_id=inbox_id), "phone_number": "+5215512345678"}

    async def get_macro_actions(self, *, macro_id: int) -> list[dict]:
        if self.macro_error is not None:
            raise self.macro_error
        return self.actions

    async def get_conversation_labels(self, *, conversation_id: int) -> list[str]:
        return []


class RuleBase(Base):
    async def get_live_keyword_rules(self, instance: dict[str, str]) -> dict[str, Any]:
        assert instance == {"tenant_ref": "lancemos", "ally_ref": "att1"}
        return {"rules": list(self.db.live_rules)}


def _app(db: RuleDb | None = None, *, chatwoot: Chatwoot | None = None,
         **overrides: Any) -> tuple[TestClient, RuleDb, SlackEmulator, _Clock]:
    db = db or RuleDb()
    slack = SlackEmulator()
    clock = _Clock()
    approval = flow_approval.ApprovalStore(base_url="https://att1-rest.internal", db_key=DB_KEY,
                                           transport=httpx.MockTransport(db.handler))
    oidc = flow_approval.SlackIdTokenClient(client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                                            transport=httpx.MockTransport(slack.handler), clock=lambda: clock.now)
    store = keyword_rule_api.KeywordRuleStore(base_url="https://att1-rest.internal", db_key=AE_KEY,
                                              transport=httpx.MockTransport(db.handler))
    settings = _settings(**{**KEYWORDS, **overrides})
    app = create_app(settings, shadow_processor=_Processor(), supabase_client=RuleBase(db),  # type: ignore[arg-type]
                     chatwoot_client=chatwoot or Chatwoot(),  # type: ignore[arg-type]
                     flow_approval_store=approval, flow_approval_oidc=oidc, keyword_rule_store=store)
    client = TestClient(app, base_url=ORIGIN)
    client.__enter__()
    return client, db, slack, clock


def _signed(db: RuleDb | None = None, **overrides: Any) -> tuple[TestClient, RuleDb]:
    client, db, slack, clock = _app(db, **overrides)
    _sign_in(client, slack, clock, user=DAN)
    return client, db


def _ae(client: TestClient, method: str, path: str, body: dict | None = None, *, token: str = TOKEN) -> httpx.Response:
    return client.request(method, f"{PREFIX}/keyword-rules{path}", json=body,
                          headers={"Authorization": f"Bearer {token}"})


def _field(page: str, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]+)"', page)
    assert match is not None, name
    return match.group(1)


def _visible(page: str) -> str:
    page = re.sub(r"<style.*?</style>", " ", page, flags=re.S)
    return re.sub(r"<[^>]+>", " ", page)


def _post(client: TestClient, action: str, **fields: str) -> httpx.Response:
    return client.post(f"{PAGE}/{action}", content=urlencode(fields), headers=FORM, follow_redirects=False)


def _done(response: httpx.Response) -> str:
    assert response.status_code == 303, response.text
    return parse_qs(urlsplit(response.headers["location"]).query)["hecho"][0]


# ------------------------------------------------------------------ el flag


def test_with_the_flag_off_nothing_exists_and_ready_does_not_change() -> None:
    client, db, *_ = _app(keyword_rules_enabled=False)
    assert _ae(client, "POST", "/preview", {"rule": _rule()}).status_code == 404
    assert client.get(PAGE, follow_redirects=False).status_code == 404
    assert db.rule_calls == []


def test_with_the_flag_ready_says_how_many_rules_are_served() -> None:
    db = RuleDb()
    db.set_draft()
    db.rule_row["status"] = "publicada"  # type: ignore[index]
    db.live_rules = [db._live_row()]
    client, *_ = _app(db)
    response = client.get("/ready")
    assert "keyword_rules" in response.text and "served=1,not_served=0,refresh=ok" in response.text


# ------------------------------------------------------------------ las rutas del AE


def test_the_ae_routes_need_its_key() -> None:
    client, *_ = _app()
    assert _ae(client, "POST", "/preview", {"rule": _rule()}, token="z" * 48).status_code == 401


def test_the_preview_validates_without_saving_and_gives_the_wa_me_link() -> None:
    client, db, *_ = _app()
    data = _ae(client, "POST", "/preview", {"rule": _rule()}).json()["data"]
    assert data["ok"] is True and data["blocks"] == []
    assert data["preview"]["wa_me"] == "https://wa.me/5215512345678?text=Hola%2C%20necesito%20soporte"
    assert data["preview"]["palabra_normalizada"] == "hola necesito soporte"
    assert db.rule_calls == []


def test_a_flow_in_the_rule_is_refused_in_v1() -> None:
    client, *_ = _app()
    response = _ae(client, "POST", "/preview", {"rule": {**_rule(), "flujo": {"producto": "p", "flujo": "f"}}})
    assert response.status_code == 422 and response.json()["detail"] == "keyword_rule_flow_not_supported"


@pytest.mark.parametrize(("changes", "chatwoot", "code"), [
    ({"etiqueta": "cliente"}, None, "keyword_rule_label_reserved"),
    ({}, Chatwoot(actions=[{"action_name": "add_label", "action_params": [LABEL]},
                           {"action_name": "send_message", "action_params": ["hola"]}]),
     "keyword_rule_macro_does_more_than_add_the_label"),
    ({}, Chatwoot(macro_error=OSError("no contesto")), "keyword_rule_macro_unreadable"),
])
def test_saving_blocks_when_something_blocks(changes: dict, chatwoot: Chatwoot | None, code: str) -> None:
    client, db, *_ = _app(chatwoot=chatwoot)
    response = _ae(client, "POST", "", {"rule": _rule(**changes), "purpose": "Soporte", "requested_by": ASKER})
    assert response.status_code == 422 and response.json()["detail"] == "keyword_rule_draft_blocked"
    assert code in [block["code"] for block in response.json()["data"]["blocks"]]
    assert db.rule_calls == []


def test_saving_returns_the_page_link_and_the_agent_has_no_route_to_publish() -> None:
    client, db, *_ = _app()
    saved = _ae(client, "POST", "", {"rule": _rule(), "purpose": "El link de soporte", "requested_by": ASKER})
    assert saved.status_code == 201, saved.text
    assert saved.json()["data"]["approval_url"] == f"{ORIGIN}{PAGE}"
    assert _ae(client, "GET", f"/{RULE}").json()["data"]["status"] == "borrador"
    assert _ae(client, "GET", "").json()["data"]["rules"][0]["rule_id"] == RULE
    paths = {route.path for route in client.app.routes}  # type: ignore[attr-defined]
    assert not any(path.startswith(f"{PREFIX}/keyword-rules") and path.endswith(("/publish", "/pause"))
                   for path in paths)


def test_a_purpose_with_a_phone_is_refused() -> None:
    client, db, *_ = _app()
    response = _ae(client, "POST", "", {"rule": _rule(), "purpose": "para el 5215512345678", "requested_by": ASKER})
    assert response.status_code == 422 and db.rule_calls == []


# ------------------------------------------------------------------ la pagina


def test_without_a_session_nothing_is_published() -> None:
    client, db, *_ = _app()
    db.set_draft()
    assert client.get(PAGE, follow_redirects=False).status_code == 401
    assert _post(client, "publish", csrf_token="x" * 64, fingerprint=FINGERPRINT).status_code == 401
    assert db.rule_row["status"] == "borrador" and db.live_rules == []  # type: ignore[index]


def test_someone_out_of_the_list_does_not_get_in() -> None:
    client, db, slack, clock = _app()
    _sign_in(client, slack, clock, user=OUTSIDER)
    db.set_draft()
    assert client.get(PAGE, follow_redirects=False).status_code == 401


def test_the_page_says_what_it_does_without_ids_and_publishing_serves_it_now() -> None:
    client, db = _signed()
    db.set_draft()
    page = client.get(PAGE).text
    visible = _visible(page)
    assert "«Hola, necesito soporte»" in visible and "soporte" in visible and "Aprobar y publicar" in page
    assert "No manda ningún mensaje ni arranca ningún flujo" in visible
    assert "wa.me/5215512345678" in visible
    assert RULE not in visible and FINGERPRINT not in visible and DAN not in visible

    assert _done(_post(client, "publish", csrf_token=_field(page, "csrf_token"),
                       fingerprint=_field(page, "fingerprint"))) == "publicada"
    assert db.rule_row["status"] == "publicada"  # type: ignore[index]
    # La recarga corre ya: el bridge la sirve sin esperar los 30 segundos.
    assert "served=1" in client.get("/ready").text


def test_publishing_is_refused_when_the_word_clashes_with_a_live_rule() -> None:
    db = RuleDb()
    db.set_draft()
    db.live_rules = [{"rule_id": "7a2b3c4d-2222-4b9f-8d2e-000000000009", "rule_key": "otra", "revision": 1,
                      "rule": keyword_rules.parse_rule(_rule(clave="otra", palabra_clave="soporte", etiqueta="otra")),
                      "fingerprint": "e" * 64, "approved_at": "2026-11-26T10:00:00Z"}]
    client, db = _signed(db)
    page = client.get(PAGE).text
    assert _done(_post(client, "publish", csrf_token=_field(page, "csrf_token"),
                       fingerprint=FINGERPRINT)) == "keyword_rule_clashes_with_rule"
    assert "approver_publish_keyword_rule_v1" not in db.rule_calls
    assert db.rule_row["status"] == "borrador"  # type: ignore[index]


def test_pause_resume_and_discard() -> None:
    client, db = _signed()
    db.set_draft()
    page = client.get(PAGE).text
    csrf = _field(page, "csrf_token")
    _post(client, "publish", csrf_token=csrf, fingerprint=FINGERPRINT)
    assert _done(_post(client, "discard", csrf_token=csrf)) == "keyword_rule_live_pause_first"
    assert _done(_post(client, "pause", csrf_token=csrf, motivo="se termino")) == "pausada"
    assert db.live_rules == [] and "served=0" in client.get("/ready").text
    assert _done(_post(client, "resume", csrf_token=csrf)) == "reanudada"
    assert "served=1" in client.get("/ready").text
    _post(client, "pause", csrf_token=csrf)
    assert _done(_post(client, "discard", csrf_token=csrf)) == "descartada"
    assert db.rule_row["status"] == "descartada"  # type: ignore[index]


def test_the_ae_macro_tool_can_create_a_macro_with_only_the_keyword_rules() -> None:
    # automation-expert-chatwoot-v1: el macro que solo suma una etiqueta, ahora tambien para las reglas.
    settings = _settings(**{**KEYWORDS, "automation_expert_chatwoot_enabled": True,
                                    "chatwoot_control_api_access_token": "t" * 32})
    assert settings.keyword_rules_enabled and not settings.agent_campaigns_enabled
    app = create_app(settings, shadow_processor=_Processor(), supabase_client=RuleBase(RuleDb()),  # type: ignore[arg-type]
                     chatwoot_client=Chatwoot())  # type: ignore[arg-type]
    paths = {(route.path, tuple(sorted(getattr(route, "methods", ()) or ()))) for route in app.routes}
    assert any(path == f"{PREFIX}/chatwoot/macros" and "POST" in methods for path, methods in paths)
